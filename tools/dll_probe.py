"""最小 .NET 元数据探针：从 Assembly-CSharp.dll 读出指定类型的字段与方法。

不依赖 mono/ilspy。只解析我们需要的三张表（TypeDef / Field / MethodDef），
目的是搞清楚 GridNavSpace / GridOccupant 这些类型的真实结构，
而不是靠 strings 猜。

用法: python3 tools/dll_probe.py <dll> <类型名> [类型名...]
"""
import struct
import sys


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def find_metadata(b):
    """定位 CLI 元数据根（签名 BSJB）。"""
    i = b.find(b"BSJB")
    if i < 0:
        raise SystemExit("找不到 BSJB 元数据签名")
    return i


def parse_streams(b, md):
    """读元数据流目录，返回 {流名: (偏移, 大小)}。"""
    ver_len = u32(b, md + 12)
    p = md + 16 + ver_len
    p += 2  # flags
    n = u16(b, p)
    p += 2
    streams = {}
    for _ in range(n):
        off = u32(b, p)
        size = u32(b, p + 4)
        p += 8
        end = b.index(b"\0", p)
        name = b[p:end].decode("ascii")
        p = end + 1
        p = (p + 3) & ~3  # 4 字节对齐
        streams[name] = (md + off, size)
    return streams


ROW_COUNT_TABLES = 64
# 需要的表编号
T_MODULE, T_TYPEREF, T_TYPEDEF, T_FIELD, T_METHODDEF = 0x00, 0x01, 0x02, 0x04, 0x06


def parse_tables(b, streams):
    """解析 #~ 表流，返回 (rows, tbl_off, heap_sizes, strings_fn)。"""
    t_off, _ = streams["#~"]
    heap_sizes = b[t_off + 6]
    valid = struct.unpack_from("<Q", b, t_off + 8)[0]
    p = t_off + 24
    rows = {}
    for i in range(ROW_COUNT_TABLES):
        if valid >> i & 1:
            rows[i] = u32(b, p)
            p += 4
    return rows, p, heap_sizes


def dump_types(b, streams, wanted):
    """列出目标类型的字段与方法。"""
    rows, data_off, heap_sizes = parse_tables(b, streams)
    s_off, _ = streams["#Strings"]

    def gstr(idx):
        e = b.index(b"\0", s_off + idx)
        return b[s_off + idx:e].decode("utf-8", "replace")

    str_wide = bool(heap_sizes & 1)
    guid_wide = bool(heap_sizes & 2)
    blob_wide = bool(heap_sizes & 4)
    SW = 4 if str_wide else 2
    GW = 4 if guid_wide else 2
    BW = 4 if blob_wide else 2

    def rd_str(o):
        return (u32(b, o) if str_wide else u16(b, o)), SW

    # 简单编码索引宽度：行数 > 2^16 则 4 字节
    def idx_w(*tables):
        total = max((rows.get(t, 0) for t in tables), default=0)
        return 4 if total >= (1 << 16) else 2

    p = data_off
    # Module (0x00): Generation(2) Name(S) Mvid(G) EncId(G) EncBaseId(G)
    if T_MODULE in rows:
        p += rows[T_MODULE] * (2 + SW + GW * 3)
    # TypeRef (0x01): ResolutionScope(coded) Name(S) Namespace(S)
    rs_w = idx_w(T_MODULE, T_TYPEREF, 0x23, 0x1A)
    if T_TYPEREF in rows:
        p += rows[T_TYPEREF] * (rs_w + SW * 2)

    # TypeDef (0x02): Flags(4) Name(S) Namespace(S) Extends(coded) FieldList Methodist
    if T_TYPEDEF not in rows:
        print("无 TypeDef 表")
        return
    ext_w = idx_w(T_TYPEDEF, T_TYPEREF, 0x1B)
    fld_w = idx_w(T_FIELD)
    mth_w = idx_w(T_METHODDEF)
    td_size = 4 + SW * 2 + ext_w + fld_w + mth_w
    td_off = p

    types = []
    for i in range(rows[T_TYPEDEF]):
        o = td_off + i * td_size
        name_i, _ = rd_str(o + 4)
        ns_i, _ = rd_str(o + 4 + SW)
        fl = u32(b, o + 4 + SW * 2 + ext_w) if fld_w == 4 else u16(b, o + 4 + SW * 2 + ext_w)
        ml_o = o + 4 + SW * 2 + ext_w + fld_w
        ml = u32(b, ml_o) if mth_w == 4 else u16(b, ml_o)
        types.append((gstr(name_i), gstr(ns_i), fl, ml))

    p = td_off + rows[T_TYPEDEF] * td_size
    # Field (0x04): Flags(2) Name(S) Signature(B)
    f_size = 2 + SW + BW
    f_off = p
    fields = []
    for i in range(rows.get(T_FIELD, 0)):
        o = f_off + i * f_size
        ni, _ = rd_str(o + 2)
        fields.append(gstr(ni))
    p = f_off + rows.get(T_FIELD, 0) * f_size

    # MethodDef (0x06): RVA(4) ImplFlags(2) Flags(2) Name(S) Sig(B) ParamList
    prm_w = idx_w(0x08)
    m_size = 4 + 2 + 2 + SW + BW + prm_w
    m_off = p
    methods = []
    for i in range(rows.get(T_METHODDEF, 0)):
        o = m_off + i * m_size
        ni, _ = rd_str(o + 8)
        methods.append(gstr(ni))

    nf, nm = len(fields), len(methods)
    for ti, (name, ns, fl, ml) in enumerate(types):
        if name not in wanted:
            continue
        # 本类型的字段/方法区间 = [本行起始, 下一行起始)
        nxt_f = types[ti + 1][2] if ti + 1 < len(types) else nf + 1
        nxt_m = types[ti + 1][3] if ti + 1 < len(types) else nm + 1
        print("\n######## %s%s ########" % (ns + "." if ns else "", name))
        print("  -- 字段 --")
        for k in range(fl - 1, min(nxt_f - 1, nf)):
            print("     ", fields[k])
        print("  -- 方法 --")
        for k in range(ml - 1, min(nxt_m - 1, nm)):
            print("     ", methods[k])


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    wanted = set(sys.argv[2:])

    with open(path, "rb") as f:
        b = f.read()

    md = find_metadata(b)
    streams = parse_streams(b, md)
    print("元数据流:", {k: v[1] for k, v in streams.items()})

    if "#Strings" not in streams:
        raise SystemExit("无 #Strings 流")
    s_off, s_size = streams["#Strings"]
    blob = b[s_off:s_off + s_size]

    # 先做一件简单可靠的事：把 #Strings 里所有名字列出来，
    # 过滤出与目标类型相关的，确认拼写与存在性。
    names = set()
    start = 0
    while True:
        e = blob.find(b"\0", start)
        if e < 0:
            break
        if e > start:
            try:
                names.add(blob[start:e].decode("utf-8"))
            except UnicodeDecodeError:
                pass
        start = e + 1

    print("#Strings 条目数:", len(names))
    dump_types(b, streams, wanted)


if __name__ == "__main__":
    main()
