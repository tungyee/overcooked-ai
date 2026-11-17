# CoChef setup for Windows

[中文](setup-windows.md) · [English README](../README.md)

## First-time setup

Clone the repository and open it in PowerShell. [Clone commands](../README.md#1-set-up)

### 1. Install the game and BepInEx

- Install the Steam version of **Overcooked! 2**.
- Install Python 3.9 or later. Use Windows PowerShell 5.1 or PowerShell 7.
- Extract **BepInEx 5 for Windows x86** into the game’s installation folder, next to `Overcooked2.exe`. Launch the game once to generate the BepInEx configuration, then exit the game. [BepInEx releases](https://github.com/BepInEx/BepInEx/releases)

### 2. Create the Python environment and install the plugin

With the game closed, run this command from the repository root. Replace the example path with your game’s installation folder.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Deploy -Voice
```

The setup script creates `.venv`, installs the voice dependencies, and builds and installs `BepInEx\plugins\Oc2AIPlugin.dll`. It backs up an existing copy of the plugin before replacing it. The build script downloads Roslyn from NuGet into `.tools` when needed. A separate .NET SDK installation is optional for Windows.

You can pass `-Python 'C:\path\to\python.exe'` to choose the interpreter used to create the virtual environment. If you already have a compatible compiler, you can use it to build and install the plugin directly.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\plugin\build.ps1 -Compiler 'C:\path\to\csc.exe' -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Deploy
```

### 3. Configure voice

Copy `.env.example` to `.env` in the repository root. Fill in the settings for the services you use.

- **Volcengine speech services** require `VOLC_ASR_API_KEY` for speech recognition and `VOLC_TTS_API_KEY` for text-to-speech.
- **Language model** uses either Volcengine Ark or Xiaomi MiMo. For Ark, fill in `ARK_API_KEY` and set `ARK_MODEL` to your model name or inference endpoint ID. For MiMo, uncomment and set `LLM_PROVIDER=mimo`, then uncomment and fill in `MIMO_API_KEY` and `MIMO_MODEL`.

The template includes links to the service consoles and examples for custom settings. [Configuration template](../.env.example)

In Windows microphone privacy settings, allow desktop apps to access the microphone. Hold F8 to speak and release it to send. If F8 conflicts with a system shortcut, set `OC2_VOICE_PTT_KEY=caps_lock` in `.env`.

## Launch the game and CoChef

Use the same game folder you used to install the plugin. You can also set `$env:GAME_DIR` in PowerShell and omit `-GameDir`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -GameDir 'D:\steam\steamapps\common\Overcooked! 2' -Voice
```

The launcher starts the game through Steam if needed, waits for local ports **27411** and **27412**, and runs CoChef in the current terminal.

1. Join as Player 1 at the title screen and go to the main menu.
2. With one human player in the menu, wait for CoChef’s virtual controller to join as Player 2.
3. Once both players are visible, choose the level you want to play in local co-op.

If the log shows `not engaged`, return to the main menu and wait for the virtual controller before re-entering the level. `[EXEC] engaged` means CoChef’s controller has joined.

## Stop, restart, and diagnose

Press **Ctrl+C** to stop CoChef and leave the game running. If the virtual controller is still connected, you can run the launcher again from inside the level.

The commands below use the default game folder. If you use a custom folder, set `$env:GAME_DIR` or pass `-GameDir` to the launcher.

```powershell
# Stop CoChef from another terminal and wait for it to exit.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop_windows.ps1 -Wait
# Wait for the managed instance to exit, then restart it with voice.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Restart -Voice
# Run with voice for 150 seconds.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Voice -Seconds 150
```

The launcher prevents a second managed instance from starting while the first one holds `agent.lock`. Use `-Restart` to request a clean exit and wait for the lock. If you started `run_agent.py` directly, stop it in its own terminal.

```powershell
# Check the game state connection for five seconds.
.\.venv\Scripts\python.exe -X utf8 tools\check_windows.py --seconds 5
# Preview CoChef's decisions for 15 seconds without controlling the game.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -DryRun -NoVoice -Seconds 15
```

## Voice console

Use the voice console if you want a terminal that stays open between runs. Press **Ctrl+C** to stop CoChef, then type `oc2` and press Enter to start it again.

```powershell
powershell.exe -NoProfile -NoExit -ExecutionPolicy Bypass -File .\voice_console.ps1
```
