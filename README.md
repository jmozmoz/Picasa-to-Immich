# Picasa to Immich

`picasa-to-immich` scans a directory tree containing Picasa metadata
and can either:

1.  **Export Picasa album membership** as lists of source-relative file
    paths, without connecting to Immich; or
2.  **Migrate Picasa album memberships to an existing Immich library**
    by matching local files to assets already stored on the Immich
    server.

The script does not upload media files. In Immich migration mode, it
creates albums or adds existing Immich assets to albums.

## Features

-   Reads `.picasa.ini` files recursively below the source directory.
-   Exports each Picasa album as a list of relative image/video paths.
-   Handles identical filenames in different directories as separate
    local files.
-   Preserves Picasa folder metadata from the `[Picasa]` section
    separately from albums and image entries.
-   Preserves all Picasa properties, including properties the script
    does not interpret.
-   Copies original `.picasa.ini` files byte-for-byte into the output
    archive in Immich migration mode.
-   Matches local files against existing Immich assets using checksums,
    filename and file size.
-   Allows one Immich asset to be referenced by local files at multiple
    paths and to belong to multiple albums.
-   Uses dry-run by default for Immich changes.

## Installation

Requires Python 3.10 or newer. Install from the project folder (or,
once published, from PyPI):

``` bash
python -m pip install .                  # from a checkout
python -m pip install picasa-to-immich   # from PyPI, once published
```

This installs the `picasa-to-immich` command and its only dependency,
`requests`. Check it with `picasa-to-immich --version`.

Without installing, the same program can be run from a checkout with
`python picasa_to_immich.py ...`. That needs `requests` only for Immich
migration mode, not for `--scan-only`.

Immich migration mode also needs an Immich server URL and API key (see
[Configuration](#configuration)).

## Input structure and Picasa metadata

The positional `source` argument must be the root directory of the
Picasa folder tree. The script searches recursively for files named
exactly `.picasa.ini`.

It recognizes three kinds of INI sections:

-   `[Picasa]`: metadata describing the physical directory, such as
    `name`, `location`, `category`, `date` and `P2category`.
-   `[.album:<token>]`: a Picasa album definition. The album's displayed
    name is taken from its `name` property, if present.
-   Other sections: image/video entries. The section name is treated as
    a filename relative to the directory containing that `.picasa.ini`.
    The `albums` property contains comma-separated album tokens.

For example, a folder metadata section such as:

``` ini
[Picasa]
name=2020-01-01 Hiking
location=Example Town
category=Folders on Disk
date=43831.980718
P2category=Folders on Disk
```

is recorded as folder metadata. It is **not** treated as an album and
does not create an Immich album.

The `date` value is retained as originally stored. The script also
attempts to convert it from a Picasa/Excel-style serial day count based
on 1899-12-30 and adds a `dateIso` value. The converted value has no
timezone information.

## 1. Scan-only mode: export album file lists

This mode reads the Picasa metadata and checks whether the referenced
image/video files exist under the source directory. It does not contact
Immich and does not require an API key.

Basic usage:

``` bash
picasa-to-immich /path/to/picasa --scan-only
```

Choose an output directory:

``` bash
picasa-to-immich /path/to/picasa \
    --scan-only \
    --output picasa_scan
```

The default output directory is `picasa_migration`.

### Scan-only output

The script creates:

-   `album_file_list.json` --- structured album, folder and warning
    information.
-   `album_file_list.txt` --- a readable text list with one album
    heading followed by its relative file paths.

Example `album_file_list.txt`:

``` text
[Holiday 2020] (token: abc123)
2020/a.jpg
2020/b.jpg

[Holiday 2021] (token: def456)
2021/c.jpg
2021/d.jpg
```

Paths are relative to the source directory, not absolute filesystem
paths. The same physical file path can appear in more than one album.
Files with the same basename in different directories remain distinct.

### JSON output structure

`album_file_list.json` contains:

-   `createdAt`: scan timestamp.
-   `source`: absolute source directory used for the scan.
-   `iniFileCount`: number of `.picasa.ini` files found.
-   `albumCount`: number of album definitions found.
-   `folderMetadataCount`: number of directories with `[Picasa]`
    metadata.
-   `imageSections`: number of image/video sections collected from the
    INI files.
-   `folders`: folder metadata records, including `relativePath`,
    original `properties`, `sourceIni` and, when conversion succeeds,
    `dateIso`.
-   `albums`: album records with `token`, `name`, existing `files`,
    `missingFiles` and `fileCount`.
-   `warnings`: parsing and metadata warnings, including undefined album
    tokens, invalid folder dates and paths outside the source directory.

A file listed under `missingFiles` is referenced by Picasa metadata but
was not found as a supported image/video file at the expected relative
path. The text output includes these paths as comments beneath the
corresponding album.

## 2. Immich migration mode

Without `--scan-only`, the script runs in Immich migration mode. It
scans the Picasa metadata, retrieves the existing Immich assets and
albums, matches local files to Immich assets, and prepares album
membership changes.

The script does **not** upload files. It only creates albums and adds
existing Immich asset IDs to albums.

### Configuration

The Immich URL and API key can be supplied as command-line arguments or
environment variables.

Linux/macOS:

``` bash
export IMMICH_URL="http://immich-server:2283"
export IMMICH_API_KEY="YOUR_API_KEY"
```

PowerShell:

``` powershell
$env:IMMICH_URL = "http://immich-server:2283"
$env:IMMICH_API_KEY = "YOUR_API_KEY"
```

Alternatively, pass `--url` and `--api-key` on the command line.

#### Using a `.env` file

Instead of environment variables, the values can be stored in a file
named `.env`:

``` ini
IMMICH_URL=http://immich-server:2283
IMMICH_API_KEY=YOUR_API_KEY
```

Without `--env-file`, the program uses the **first** of these files
that exists (only one file is read):

1.  `.env` in the current working directory.
2.  `.env` in your user configuration directory:

    | System | Location |
    |---|---|
    | Windows | `%APPDATA%\picasa-to-immich\.env` |
    | Linux | `$XDG_CONFIG_HOME/picasa-to-immich/.env`, default `~/.config/picasa-to-immich/.env` |
    | macOS | `~/Library/Application Support/picasa-to-immich/.env` |

The configuration directory is the usual place once the program is
installed; create the folder if it does not exist. `picasa-to-immich
--help` shows both paths for your system. Use `--env-file PATH` to
read any other file. `.env.example` in the source checkout is a
template. Blank lines, `#` comments, an `export ` prefix and quoted
values are accepted.

Windows (PowerShell):

``` powershell
New-Item -ItemType Directory -Force "$env:APPDATA\picasa-to-immich"
notepad "$env:APPDATA\picasa-to-immich\.env"
```

If a value is given in more than one place, the command-line option
wins, then the environment variable, then the `.env` file.

The file contains the API key: keep it private. In the source checkout
`.env` is listed in `.gitignore` and is never included in the package.

#### Creating the required API key

1.    Log in to the Immich web interface.
1.    Open Account Settings → API Keys.
1.    Select New API Key.
1.    Give it a name, e.g. Picasa album migration.
1.    Copy the generated key.

#### Required permissions

For recent Immich versions with granular API-key permissions:

|Permission|Purpose|
|---|----|
|`asset.read`|Search existing assets and read metadata|
|`album.read`|	List albums and their contents|
|`album.create`|Create new albums|
|`albumAsset.create`|Add assets to albums|
|`server.about`|Optional: retrieve server version information|

For dry-run, the essential permissions are `asset.read` and `album.read`. The other two album permissions are needed when applying changes.

No upload or delete permissions are required.

### Dry run (default)

``` bash
picasa-to-immich /path/to/picasa \
    --url http://immich-server:2283 \
    --api-key YOUR_API_KEY
```

A dry run still connects to Immich and reads its assets and albums. It
writes the migration reports but does not create albums or add assets.

Review `migration_report.json` before applying changes.

### Apply changes

``` bash
picasa-to-immich /path/to/picasa \
    --url http://immich-server:2283 \
    --api-key YOUR_API_KEY \
    --apply
```

`--apply` creates missing albums and adds matched assets to existing
albums when they are not already members. Existing albums are identified
by case-insensitive album name. If more than one Immich album has the
same name, the script reports the ambiguity and does not choose one
automatically.

### Asset matching

For each local image/video entry, the script calculates its SHA-1
checksum and file size. It then attempts to match it to the Immich asset
inventory in this order:

1.  **Checksum:** a unique matching server checksum is accepted as a
    high-confidence match. Multiple checksum matches are reported as
    ambiguous.
2.  **Filename and size:** if there is no checksum match, assets with
    the same original filename and matching file size are considered. A
    unique match is accepted.
3.  **Filename only (optional):** with `--allow-name-only`, a unique
    same-name asset can be accepted as a lower-confidence match.

A same-name asset with a known, different checksum is rejected rather
than accepted on file size alone. Ambiguous matches are reported and are
not automatically selected.

#### External libraries

For assets in an Immich **external library**, Immich does not store a
checksum of the file content. It stores the SHA-1 of the text
`path:` followed by the asset's path on the server (`originalPath`).
Such a checksum can never equal the checksum of the local file.

The script recognizes these path-based checksums and treats them as
unknown. External-library assets are therefore matched by **filename
and size** (step 2). In `migration_report.json` these matches have the
method `filename_and_size_path_based_server_checksum`.

The file size comes from the asset's EXIF data
(`exifInfo.fileSizeInByte`), which the script requests from Immich.

Files whose type is excluded from the external library, for example by
an exclusion pattern such as `**/*.DNG`, never become Immich assets.
They are reported as `unmatched` with reason `no_candidate`. If such a
file is a Picasa album member, either remove the exclusion pattern and
rescan the library, or add the corresponding asset (such as a JPEG
version) to the album by hand.

#### Match reasons in the report

| Status | Reason / method | Meaning |
|---|---|---|
| `matched` | `sha1` | Content checksum matches |
| `matched` | `filename_and_size_no_server_checksum` | Server has no checksum; name and size match |
| `matched` | `filename_and_size_path_based_server_checksum` | External-library asset; name and size match |
| `matched_low_confidence` | `filename_only_explicitly_allowed` | Name only, with `--allow-name-only` |
| `unmatched` | `server_checksum_mismatch` | Same name, but a different content checksum |
| `unmatched` | `no_candidate` | No Immich asset with this name |
| `unmatched` | `local_file_not_found` | Referenced in Picasa, but missing locally |
| `ambiguous` | `multiple_*` | Several candidates; none is chosen |

The same Immich asset may match more than one local path. Its UUID is
reused for all those paths, and its album memberships are combined.
Duplicate asset IDs are removed within each album.

### Important matching considerations

-   The script expects each image/video section filename in a
    `.picasa.ini` file to resolve relative to the directory containing
    that INI file.
-   Paths are checked to ensure they remain inside the specified source
    directory.
-   Matching is based on file content checksum where possible, not on
    directory names.
-   `--allow-name-only` weakens matching confidence; use it only when
    that trade-off is acceptable.
-   The script does not apply Picasa editing instructions such as
    rotation, crop or filters to the Immich asset. Those properties are
    preserved in the metadata archive as pending edits.

## Command-line options

``` text
usage: picasa-to-immich [-h] [--version] [--output OUTPUT] [--scan-only]
                        [--url URL] [--api-key API_KEY] [--env-file ENV_FILE]
                        [--apply] [--allow-name-only] [--no-verify-tls]
                        [--page-size PAGE_SIZE] [--timeout TIMEOUT]
                        [--log-level {DEBUG,INFO,WARNING,ERROR}] [--debug-api]
                        source
```

  -----------------------------------------------------------------------
  Option                              Description
  ----------------------------------- -----------------------------------
  `source`                            Required root directory containing
                                      the Picasa folder tree and
                                      `.picasa.ini` files.

  `--output OUTPUT`                   Output/archive directory. Default:
                                      `picasa_migration`.

  `--scan-only`                       Export album file lists only. Does
                                      not contact Immich and does not
                                      need an API key.

  `--url URL`                         Immich base URL, for example
                                      `http://immich-server:2283`. Can
                                      also be set with `IMMICH_URL`.

  `--api-key API_KEY`                 Immich API key. Can also be set
                                      with `IMMICH_API_KEY`.

  `--apply`                           Actually create albums and add
                                      assets. Without this option, Immich
                                      mode is a dry run. Cannot be
                                      combined with `--scan-only`.

  `--allow-name-only`                 Allow unique filename-only matches
                                      as lower-confidence matches.

  `--no-verify-tls`                   Disable TLS certificate
                                      verification. Use only when
                                      necessary, such as with a
                                      deliberately untrusted/self-signed
                                      certificate.

  `--page-size PAGE_SIZE`             Immich search page size. Default:
                                      `500`; must be positive.

  `--timeout TIMEOUT`                 HTTP request timeout in seconds.
                                      Default: `60`.

  `--log-level LEVEL`                 Logging verbosity: `DEBUG`, `INFO`,
                                      `WARNING` or `ERROR`. Default:
                                      `INFO`.

  `--env-file ENV_FILE`               File with `IMMICH_URL` and
                                      `IMMICH_API_KEY` lines. Default:
                                      `.env` in the current directory,
                                      else in the user configuration
                                      directory (see above).

  `--debug-api`                       Log every Immich API request
                                      (method, URL, query, JSON body) and
                                      its response status. The API key
                                      is never logged.
  -----------------------------------------------------------------------

## Output files in Immich migration mode

The output directory contains:

  -----------------------------------------------------------------------
  File/directory                      Contents
  ----------------------------------- -----------------------------------
  `asset_mapping.json`                Mapping from each source-relative
                                      local path key to its Immich asset
                                      ID and match details.

  `picasa_migration_metadata.json`    Archived Picasa properties and
                                      album memberships grouped by Immich
                                      asset ID, plus album and folder
                                      metadata.

  `migration_report.json`             Run mode, Immich version, match
                                      results, album actions, counts and
                                      warnings.

  `original_ini/`                     Byte-for-byte copies of the
                                      `.picasa.ini` files, preserving
                                      their relative directory structure.
  -----------------------------------------------------------------------

The scan-only mode writes `album_file_list.json` and
`album_file_list.txt` instead. It does not make an Immich connection or
copy the INI files into `original_ini/`.

## Safety and recommended workflow

1.  Keep an untouched backup of the Picasa source directory.

2.  Run scan-only mode first if you want to inspect album membership
    without involving Immich:

    ``` bash
    picasa-to-immich /path/to/picasa --scan-only
    ```

3.  Inspect `album_file_list.txt` and `album_file_list.json`, especially
    missing files and warnings.

4.  Run Immich migration mode without `--apply`.

5.  Review `migration_report.json`, including unmatched and ambiguous
    files and planned album changes.

6.  Only when the report is satisfactory, rerun with `--apply`.

The script writes reports and archives into the output directory. Choose
an output directory outside the source tree to keep generated files
separate from the Picasa collection.

## Troubleshooting

-   **`Set --url and --api-key...`**: Immich migration mode requires
    both values. Use `--scan-only` to run without them.
-   **`No module named requests`**: Only when running
    `picasa_to_immich.py` without installing. Install the dependency
    with `python -m pip install requests`; it is needed only for Immich
    migration mode.
-   **The `.env` file is not found**: Run `picasa-to-immich --help`;
    the `--env-file` description lists the paths that are searched.
-   **Files appear under `missingFiles`**: Check that the files exist
    and that the image section filename is relative to the directory
    containing its `.picasa.ini`.
-   **A match is ambiguous**: The script found multiple possible Immich
    assets. It deliberately does not select one automatically; inspect
    the candidate IDs in `migration_report.json`. With external
    libraries this usually means the same file exists twice in Immich,
    for example in two library folders or as an upload plus the library
    copy. Open `http://<immich-server>/photos/<id>` to compare them.
-   **A file is unmatched**: Check the local file path, Immich asset
    inventory, checksum and original filename. Filename-only matching
    can be enabled with `--allow-name-only`, but it is lower confidence.
-   **Many `server_checksum_mismatch` results with an external
    library**: This was caused by Immich's path-based checksums and is
    handled by current versions of the script (see
    [External libraries](#external-libraries)).
-   **`no_candidate` for a whole file type (e.g. all `.DNG` files)**:
    Check the external library's exclusion patterns in Immich.
-   **Seeing what the script sends to Immich**: Add `--debug-api` to
    log every request and its response status.

## Limitations

-   The script migrates album membership, not media files.
-   Picasa edits (rotation, crop, filters and redo information) are
    archived but not applied to Immich.
-   Folder metadata from `[Picasa]` is preserved as metadata; it does
    not create Immich albums.
-   Album definitions are kept distinct by Picasa token. Different
    tokens with identical names are not automatically merged.
-   The script relies on the Immich API response shapes it recognizes
    for asset and album searches. If an Immich upgrade changes those API
    responses, migration may stop rather than silently use an incomplete
    asset inventory.

## Development and tests

### Code layout

The code is in the `picasa_immich` package. `pyproject.toml` describes
the distribution `picasa-to-immich` and its `picasa-to-immich` command
(`picasa_immich.cli:run`). `picasa_to_immich.py` only runs the same
command from a checkout without installing.

| Module | Contents |
|---|---|
| `settings.py` | `.env` reading and the URL/API-key priority |
| `util.py` | Shared helpers: JSON output, path keys, timestamps |
| `picasa.py` | `PicasaLibrary`: reads `.picasa.ini` files; no Immich dependency |
| `scan_report.py` | Scan-only mode: album file lists |
| `matching.py` | `AssetIndex` and matching local files to Immich assets; no network |
| `immich.py` | `ImmichAPI`: the Immich REST client |
| `migration.py` | `Migration`: matching, album sync and the migration reports |
| `cli.py` | Command-line options and `main()` |

### Tests

The tests use [pytest](https://pytest.org) and never contact an Immich
server: Immich is replaced by an in-memory fake, and Picasa folders are
created in temporary directories. They also ignore any real `.env` file
and `IMMICH_*` environment variables.

``` bash
python -m pip install -e ".[test]"   # editable install with pytest
python -m pytest
```

| File | Covers |
|---|---|
| `tests/test_settings.py` | `.env` parsing and the priority of URL/API-key sources |
| `tests/test_picasa.py` | Reading `.picasa.ini` files |
| `tests/test_scan_report.py` | Scan-only output |
| `tests/test_matching.py` | Matching local files to Immich assets |
| `tests/test_immich_api.py` | API response parsing and pagination |
| `tests/test_migration.py` | Dry run, `--apply` and the command line, end to end |
| `tests/test_package.py` | Module independence and the entry script |

The `test/` directory (singular) holds example output from a real run
and is not part of the test suite.

### Building and publishing

``` bash
python -m pip install build twine
python -m build             # creates dist/*.whl and dist/*.tar.gz
python -m twine check dist/*
python -m twine upload dist/*
```

The version is `__version__` in `picasa_immich/__init__.py`.
`MANIFEST.in` adds the tests, `picasa_to_immich.py` and `.env.example`
to the source archive and keeps `test/` and `backup/` out of it. Before
the first upload, choose a license (see below) and add it to
`pyproject.toml`.

## License

No license is specified by this script. Add a license file and update
this section if you intend to distribute it.
