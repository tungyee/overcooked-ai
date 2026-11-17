# CoChef setup for macOS

[中文](setup.md) · [English README](../README.md)

## First-time setup

Clone the repository and open it in your terminal. [Clone commands](../README.md#1-set-up)

### 1. Install the game and build tools

- Install the Steam version of **Overcooked! 2** for macOS.
- Install Python 3.9 or later and make sure `python3` is available in your terminal.
- Install the .NET SDK. CoChef uses the SDK’s Roslyn compiler to build the plugin. Run `dotnet --list-sdks` to check that the SDK is installed.

### 2. Install BepInEx and doorstop

Download the **BepInEx 5.4.x Unity Mono x64 package for macOS/Unix**. The archive contains `run_bepinex.sh`, `libdoorstop.dylib`, `BepInEx/`, and `.doorstop_version`. CoChef has been tested with BepInEx 5.4.23.5 and doorstop 4.5.0. [BepInEx releases](https://github.com/BepInEx/BepInEx/releases)

Extract the archive into the game’s installation folder. To verify the installation, run the commands below. If you use a custom Steam library, replace the example path with your game folder.

```bash
export GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
ls "$GAME_DIR/BepInEx/core/BepInEx.Preloader.dll" "$GAME_DIR/libdoorstop.dylib"
```

### 3. Set up Python and voice

Create the virtual environment and install the voice dependencies from the repository root.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-voice.txt
```

Copy `.env.example` to `.env` in the repository root. Fill in the settings for the services you use.

- **Volcengine speech services** require `VOLC_ASR_API_KEY` for speech recognition and `VOLC_TTS_API_KEY` for text-to-speech.
- **Language model** uses either Volcengine Ark or Xiaomi MiMo. For Ark, fill in `ARK_API_KEY` and set `ARK_MODEL` to your model name or inference endpoint ID. For MiMo, uncomment and set `LLM_PROVIDER=mimo`, then uncomment and fill in `MIMO_API_KEY` and `MIMO_MODEL`.

The template includes links to the service consoles and examples for custom settings. [Configuration template](../.env.example)

In **System Settings → Privacy & Security**, grant Microphone and Accessibility permissions to the terminal app you use to run CoChef. If push-to-talk still doesn’t work, check Input Monitoring as well. Hold F8 to speak and release it to send. Use Fn+F8 if your keyboard treats F8 as a media key.

## Launch the game and CoChef

Run this command from the repository root.

```bash
./restart.sh --voice
```

The launcher builds and installs the plugin, starts the game through doorstop, and starts CoChef once the plugin is ready. Launch the game through this script so doorstop can load the plugin.

At the title screen, press A or Space to join as Player 1. Wait for CoChef’s virtual controller to join as Player 2 in the main menu, then start a local co-op level.

## Stop and restart

- Press **Ctrl+C** in the terminal to stop CoChef before quitting the game.
- To restart only CoChef while its controller is still connected, run `.venv/bin/python run_agent.py --voice`. The full `restart.sh` command restarts the game too.
- If you use a custom Steam library, set `GAME_DIR` in each terminal session you use to launch CoChef, or add it to your shell configuration.
- To use a different Python interpreter, set `PYTHON` to its full path before running `restart.sh`.
- To test game control without voice, run `./restart.sh --no-voice`.

If the game starts but CoChef cannot connect, check whether the plugin is listening on local ports **27411** and **27412**. [FAQ](../README.md#faq)
