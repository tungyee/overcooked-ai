#!/bin/bash
# 编译 C# BepInEx 插件 Oc2AIPlugin.cs -> Oc2AIPlugin.dll。
#
# 无 csproj/解决方案：直接用 .NET SDK 自带的 Roslyn csc，-nostdlib 引用
# 游戏本体与 BepInEx 的程序集编译（沿用 legacy oc/AGENTS.md §5.1 的方式）。
#
# 用法：  ./plugin/build.sh
# 产物：  plugin/build/Oc2AIPlugin.dll
# 部署：  把产物拷到游戏 BepInEx/plugins/Oc2AIPlugin.dll 后重启游戏才生效。
set -euo pipefail

# 游戏安装根目录，可用 GAME_DIR 环境变量覆盖。
G="${GAME_DIR:-$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2}"

# .NET 运行时与 Roslyn 编译器，可用 DOTNET / CSC_DLL 环境变量覆盖。
# 未指定时自动探测：dotnet 优先取 PATH 上的、否则回落到 ~/.dotnet；
# csc.dll 从 dotnet --list-sdks 探测，兼容官方安装器、Homebrew 与 ~/.dotnet。
DOTNET="${DOTNET:-$(command -v dotnet 2>/dev/null || echo "$HOME/.dotnet/dotnet")}"
if [ -n "${CSC_DLL:-}" ]; then
  SDK="$CSC_DLL"
else
  SDK=""
  while IFS= read -r sdk_line; do
    sdk_version="${sdk_line%% *}"
    sdk_root="${sdk_line#*\[}"
    sdk_root="${sdk_root%\]*}"
    sdk_path="$sdk_root/$sdk_version/Roslyn/bincore/csc.dll"
    if [ -f "$sdk_path" ]; then SDK="$sdk_path"; fi
  done < <("$DOTNET" --list-sdks 2>/dev/null || true)
fi
[ -n "${SDK:-}" ] || { echo "找不到 Roslyn csc.dll，请用 CSC_DLL 环境变量指定其路径" >&2; exit 1; }

M="$G/Overcooked2.app/Contents/Resources/Data/Managed"
BE="$G/BepInEx/core"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/plugin/build"

mkdir -p "$OUT"
"$DOTNET" "$SDK" -nostdlib -noconfig -target:library \
  -r:"$M/mscorlib.dll" -r:"$M/System.dll" -r:"$M/System.Core.dll" \
  -r:"$M/UnityEngine.dll" -r:"$M/UnityEngine.CoreModule.dll" \
  -r:"$M/UnityEngine.PhysicsModule.dll" \
  -r:"$M/Assembly-CSharp.dll" -r:"$M/Assembly-CSharp-firstpass.dll" \
  -r:"$BE/BepInEx.dll" \
  -out:"$OUT/Oc2AIPlugin.dll" \
  "$ROOT"/plugin/*.cs

echo "built: $OUT/Oc2AIPlugin.dll"
echo "deploy: cp \"$OUT/Oc2AIPlugin.dll\" \"$G/BepInEx/plugins/Oc2AIPlugin.dll\"  # 然后重启游戏"
