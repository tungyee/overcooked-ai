"""Assembly-CSharp.dll 反编译探针（dnfile + dncil）。

比 dll_probe.py 更进一步：不仅能列类型/字段/方法，还能把方法体反汇编成
IL 指令并解析 MemberRef/MethodDef/Field/Type/字符串 token。用途：搞清
GridNavSpace / LevelBounds / RespawnCollider 这类导航与坠落机制的运行时
数据结构，判断「无地板数据」能否在插件里反射拿到。

用法:
    python3 tools/decompile.py list <关键字...>        # 列匹配类型
    python3 tools/decompile.py dump <类型名...>        # 字段+方法签名
    python3 tools/decompile.py il <类型名> <方法名>    # 反汇编指定方法
"""
import struct
import sys

import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import read_method_body_from_bytes

DLL = ("/Users/dongyi/Library/Application Support/Steam/"
       "steamapps/common/Overcooked! 2/Overcooked2.app/"
       "Contents/Resources/Data/Managed/Assembly-CSharp.dll")

TABLES = {
    0x00: "Module", 0x01: "TypeRef", 0x02: "TypeDef", 0x03: "FieldPtr",
    0x04: "Field", 0x05: "MethodPtr", 0x06: "MethodDef", 0x07: "ParamPtr",
    0x08: "Param", 0x09: "InterfaceImpl", 0x0A: "MemberRef", 0x0B: "Constant",
    0x0C: "CustomAttribute", 0x0D: "FieldMarshal", 0x0E: "DeclSecurity",
    0x0F: "ClassLayout", 0x10: "FieldLayout", 0x11: "StandAloneSig",
    0x12: "EventMap", 0x13: "EventPtr", 0x14: "Event", 0x15: "PropertyMap",
    0x16: "PropertyPtr", 0x17: "Property", 0x18: "MethodSemantics",
    0x19: "MethodImpl", 0x1A: "ModuleRef", 0x1B: "TypeSpec", 0x1C: "ImplMap",
    0x1D: "FieldRVA", 0x1E: "ENCLog", 0x1F: "ENCMap", 0x20: "Assembly",
    0x23: "AssemblyRef", 0x26: "File", 0x27: "ExportedType", 0x28: "ManifestResource",
    0x29: "NestedClass", 0x2A: "GenericParam", 0x2B: "MethodSpec",
    0x2C: "GenericParamConstraint", 0x70: "UserString",
}


class Dll:
    def __init__(self, path=DLL):
        self.path = path
        self.pe = dnfile.dnPE(path)
        self.mt = self.pe.net.mdtables

    # ---------- 名称解析 ----------
    def tdor_name(self, encoded):
        # 入参既可能是原始 coded index int，也可能是 dnfile 的 coded index 对象。
        if hasattr(encoded, "table") and hasattr(encoded, "row_index"):
            try:
                r = encoded.table.rows[encoded.row_index]
                return str(r.TypeNamespace) + "." + str(r.TypeName)
            except Exception:
                return "<tdor?>"
        tag = encoded & 0x3
        idx = encoded >> 2
        try:
            if tag == 0:
                r = self.mt.TypeDef.rows[idx - 1]
                return str(r.TypeNamespace) + "." + str(r.TypeName)
            if tag == 1:
                r = self.mt.TypeRef.rows[idx - 1]
                return str(r.TypeNamespace) + "." + str(r.TypeName)
            if tag == 2:
                r = self.mt.TypeSpec.rows[idx - 1]
                return "<TypeSpec " + self.sig_name(r.Signature) + ">"
        except Exception:
            pass
        return "<tdor?>"

    def type_sig_name(self, data, o=0):
        e = data[o]
        o += 1
        prims = {0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "i1",
                 0x05: "u1", 0x06: "i2", 0x07: "u2", 0x08: "i4", 0x09: "u4",
                 0x0A: "i8", 0x0B: "u8", 0x0C: "r4", 0x0D: "r8", 0x0E: "string",
                 0x1C: "object"}
        if e in prims:
            return prims[e], o
        if e in (0x11, 0x12):  # valuetype/class TypeDefOrRef
            v, o = self.read_coded(data, o)
            return ("valuetype " if e == 0x11 else "") + self.tdor_name(v), o
        if e == 0x1D:  # szarray
            inner, o = self.type_sig_name(data, o)
            return inner + "[]", o
        if e in (0x13, 0x1E):  # var/mvar
            return ("!" + str(data[o])), o + 1
        if e == 0x15:  # genericinst
            kind, o = self.type_sig_name(data, o)
            n = data[o]
            o += 1
            args = []
            for _ in range(n):
                a, o = self.type_sig_name(data, o)
                args.append(a)
            return kind + "<" + ",".join(args) + ">", o
        if e in (0x1F, 0x20):  # cmod
            _, o = self.read_coded(data, o)
            return self.type_sig_name(data, o)
        if e in (0x0F, 0x10):  # ptr/byref
            inner, o = self.type_sig_name(data, o)
            return inner + ("&" if e == 0x10 else "*"), o
        if e == 0x14:  # array
            inner, o = self.type_sig_name(data, o)
            return inner + "[,]", o
        return "<elem 0x%02x>" % e, o

    @staticmethod
    def read_coded(data, o):
        # 本程序集的 TypeDefOrRef 行数 < 2^14，恒 2 字节。
        v = data[o] | (data[o + 1] << 8)
        return v, o + 2

    def sig_name(self, sig):
        data = sig.value if hasattr(sig, "value") else bytes(sig)
        return self._render_sig(data)

    def _render_sig(self, data):
        try:
            if data[0] == 0x06:  # FIELD
                t, _ = self.type_sig_name(data, 1)
                return t
            # method sig
            o = 0
            flags = data[o]
            o += 1
            if flags & 0x20:
                o += 1
            n = data[o]
            o += 1
            ret, o = self.type_sig_name(data, o)
            params = []
            for _ in range(n):
                p, o = self.type_sig_name(data, o)
                params.append(p)
            return ret + " (" + ", ".join(params) + ")"
        except Exception as e:
            return "<sig? %s>" % e

    # ---------- token 解析 ----------
    def memberref_name(self, rid):
        r = self.mt.MemberRef.rows[rid - 1]
        cls = self.tdor_name(r.Class)
        sig = self.sig_name(r.Signature)
        return "%s::%s" % (cls, str(r.Name)) + (" " + sig if sig else "")

    def methoddef_name(self, rid):
        r = self.mt.MethodDef.rows[rid - 1]
        return str(r.Name) + " " + self.sig_name(r.Signature)

    def field_name(self, rid):
        r = self.mt.Field.rows[rid - 1]
        return str(r.Name) + " " + self.sig_name(r.Signature)

    def token_name(self, tok):
        table = tok.table
        rid = tok.rid
        try:
            if table == 0x0A:
                return self.memberref_name(rid)
            if table == 0x06:
                return self.methoddef_name(rid)
            if table == 0x04:
                return self.field_name(rid)
            if table == 0x02:
                r = self.mt.TypeDef.rows[rid - 1]
                return "type %s.%s" % (r.TypeNamespace, r.TypeName)
            if table == 0x01:
                r = self.mt.TypeRef.rows[rid - 1]
                return "type %s.%s" % (r.TypeNamespace, r.TypeName)
            if table == 0x1B:
                r = self.mt.TypeSpec.rows[rid - 1]
                return "typespec " + self.sig_name(r.Signature)
            if table == 0x2B:
                return self.methodspec_name(rid)
        except Exception as e:
            return "<tok err %s>" % e
        return "tok 0x%08x" % int(tok)

    def methodspec_name(self, rid):
        r = self.mt.MethodSpec.rows[rid - 1]
        try:
            mrow = r.Method.table.rows[r.Method.row_index]
            base = "%s::%s" % (self.tdor_name(getattr(mrow, "Class", mrow)),
                               str(mrow.Name))
        except Exception:
            base = "<?method?>"
        try:
            data = r.Instantiation.value if hasattr(r.Instantiation, "value") else bytes(r.Instantiation)
            t, _ = self.type_sig_name(data, 0)
            inst = t
        except Exception:
            inst = "?"
        return "methodspec %s < %s >" % (base, inst)

    def userstring(self, off):
        try:
            return str(self.pe.net.user_strings.get(off))
        except Exception:
            return "<?str?>"

    # ---------- 类型枚举 ----------
    def find_types(self, keys):
        out = []
        for t in self.mt.TypeDef.rows:
            name = str(t.TypeName)
            ln = name.lower()
            if any(k in ln for k in keys):
                out.append((str(t.TypeNamespace), name))
        out.sort()
        return out

    def get_type(self, name):
        for t in self.mt.TypeDef.rows:
            if str(t.TypeName) == name:
                return t
        return None

    def fields(self, t):
        return [idx.row for idx in t.FieldList]

    def methods(self, t):
        return [idx.row for idx in t.MethodList]

    # ---------- IL 反汇编 ----------
    def disasm(self, type_name, method_name):
        t = self.get_type(type_name)
        if t is None:
            raise SystemExit("找不到类型 " + type_name)
        m = None
        for row in self.methods(t):
            if str(row.Name) == method_name:
                m = row
                break
        if m is None:
            raise SystemExit("找不到方法 %s.%s" % (type_name, method_name))
        rva = int(m.Rva)
        off = self.pe.get_offset_from_rva(rva)
        with open(self.path, "rb") as f:
            f.seek(off)
            blob = f.read(0x10000)
        body = read_method_body_from_bytes(blob)
        print("== %s.%s (RVA 0x%x, code_size=%d) ==" % (
            type_name, method_name, rva, body.code_size))
        for insn in body.instructions:
            op = insn.mnemonic if isinstance(insn.mnemonic, str) else str(insn.mnemonic)
            opd = insn.operand
            txt = ""
            if opd is not None:
                cls = opd.__class__.__name__
                if cls == "StringToken":
                    txt = '"' + self.userstring(opd.rid) + '"'
                elif cls == "Token":
                    txt = self.token_name(opd)
                else:
                    txt = repr(opd)
            print("  %04X  %-12s %s" % (insn.offset, op, txt))


def cmd_list(d, args):
    for ns, name in d.find_types(args):
        print("%s.%s" % (ns, name))


def cmd_dump(d, args):
    for name in args:
        t = d.get_type(name)
        if t is None:
            print("!! 找不到类型 %s" % name)
            continue
        print("=" * 70)
        print("TYPE %s.%s" % (str(t.TypeNamespace), str(t.TypeName)))
        print("  -- 字段 --")
        for f in d.fields(t):
            print("    %-32s %s" % (str(f.Name), d.sig_name(f.Signature)))
        print("  -- 方法 --")
        for m in d.methods(t):
            print("    %s%s" % (str(m.Name), d.sig_name(m.Signature)))


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    d = Dll()
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "list":
        cmd_list(d, args)
    elif cmd == "dump":
        cmd_dump(d, args)
    elif cmd == "il":
        if len(args) < 2:
            raise SystemExit("il 需要 <类型名> <方法名>")
        d.disasm(args[0], args[1])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
