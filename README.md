# imagipatcher

A patch for **Skylanders Imaginators** (Xbox 360) that adds the content released only on
the Wii U version: two characters with their variants and their vehicles. It is the Imaginators
counterpart of [superpatcher](https://github.com/SkyP4tcher/superpatcher) and uses the same converters.

The script contains no game files. It takes the content from the user's own extracted Wii U copy,
converts it to the Xbox 360 format and inserts it into the user's own Xbox 360 game files. Models,
textures, animations, audio and text are adapted to the platform, so the figures are recognized and
playable.

## Compatibility

The patched game is meant for
[Skylanders: Core of Light](https://github.com/tsapZ/SkylandersCoreOfLight), the project that brings
the Xbox 360 versions of the series to PC.

Tested with the European Xbox 360 release of Imaginators (Italian and Spanish) on Core of Light
0.6.0-beta.2: both characters load and play, and both vehicles race. The languages are detected from the Xbox 360 game, so the
script also works with other releases, which however have not been tested in game.

## Known limits

- The character and vehicle data slots are too small for these toys on Xbox 360. The script raises them
  by 1.5 MB and 1 MB in `rom/MemoryPoolConfigs.igx`. All these pools share a fixed memory area, so the
  space is taken from the Story mode sound sample pool (18 MB to 15 MB). No missing sounds have been
  noticed so far. Core of Light builds that read `game_heap_extra` (`["Mods"]` in
  `imaginatorsrecomp.toml`) get a bigger area instead: `--install` and `--apply-config` set
  `game_heap_extra = 0x400000`, make `MP_GAME_MODE_RESIZABLE` 3 MB bigger and leave the sound pool at
  18 MB. This has not been tested in game yet, since that build is not out.
- Imaginators has no driver challenges, so the Wii U challenge files are not used.
- Both vehicles have been raced; other uses have not been checked.
- Two players at once have not been tested.

## Requirements

- Skylanders Imaginators for Wii U, extracted: either the game folder (containing `content/archives`)
  or just the `archives` folder. The European release is needed to get every language.
- Skylanders Imaginators for Xbox 360 installed with Core of Light, or the game's `archives` folder.
- Python 3 with `numpy` and `scipy`.
- `ffmpeg` with the `xma2` decoder, used to verify the converted audio.

The script does not decrypt or download anything and only reads the files it is given.

## Usage

Install into the Core of Light folder itself, and undo it:

```
python3 imagipatcher.py --wiiu "<extracted Imaginators Wii U>" --install "<Core of Light Imaginators folder>"
python3 imagipatcher.py --uninstall "<Core of Light Imaginators folder>"
```

- `--install` converts, copies the files into `rom/archives` and sets the memory pools, the video
  memory budget and `portal_menu_nintendo`. Everything it replaces goes into `imagipatcher-backup` in
  the game folder, with a `manifest.json` of every change. Running it again first puts the originals
  back, so it also updates an install.
- `--uninstall` puts the original files and settings back and removes the backup folder. A file that
  something else changed after the install (a Core of Light update, a file check) is left as it is.
- While working the script prints lines like `PROGRESS 5/11 sound banks: Bowser`, for a front end
  that shows how far it is.

Or write the converted files to a folder, optionally with a separate test installation:

```
python3 imagipatcher.py --wiiu "<extracted Imaginators Wii U>" \
    --x360 "<Core of Light folder with Imaginators>" \
    --output <folder> [--test-copy "<new folder>"]
```

- `--output`: folder that receives the modified files, in `archives/`, and a `summary.txt`.
- `--test-copy`: creates a new installation next to the original one.
  - The original files are linked, not copied or modified.
  - It sets the memory pools and the video memory budget, and enables `portal_menu_nintendo` in the
    `.toml`, the Core of Light option that shows these figures in the portal menu.
  - It adds a launcher `.bat` that uses separate save files.
- `--languages es,it`: forces the languages instead of detecting them from the Xbox 360 game.
- `--apply-config "<Core of Light folder>"`: only sets `rom/MemoryPoolConfigs.igx`,
  `rom/VramBudget.igx` and `portal_menu_nintendo` in an existing installation, keeping the originals as
  `.orig`. Run it again if a Core of Light update replaces those files.

Every file written is checked again: format, archive indexes and, for audio, a full decode with ffmpeg.
If a check fails the script stops.

## How it works

- **Archives and resources**: `.pak` archives and `.igz` resources (Alchemy format) are converted from
  Wii U to Xbox 360: textures (GX2 to Xenos tiling), vertex and index buffers, vertex formats, and text
  for the languages present in the Xbox 360 game.
- **Figure registry**: the figures are added to the game's registry in `permanent.pak`.
- **Audio**: sound banks are re-encoded to XMA2 with the encoder from superpatcher (`xma.py`). Each bank
  is sized for the Imaginators slots: 2 MB for the main bank, 1 MB for the voices.
- **Memory**: the character slot and the pool that pays for it are set in `MemoryPoolConfigs.igx`; the
  video memory budget for characters and vehicles is raised in `VramBudget.igx`.

## License

GNU Lesser General Public License 2.1 or later (see `LICENSE`). The audio encoder uses the coding tables
and frame structure of the WMA Pro decoder from [FFmpeg](https://ffmpeg.org/), distributed under the
same license.

## Legal notice

Hobby project, not affiliated with or endorsed by the owners of the games and trademarks mentioned. All
trademarks belong to their respective owners. Legitimate copies of both versions of the game are
required. The repository does not contain or distribute game files, keys or tools to decrypt them.
