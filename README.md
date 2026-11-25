# dcheck

Check lots of Discord usernames at once and find out which ones are free. A small command-line tool written in plain Python, with nothing extra to install.

## Quick start (Windows, no Python needed)

1. Download **`dcheck.exe`** from the [Releases](../../releases) page.
2. Put it in a folder of its own, for example `Desktop\dcheck`.
3. In that folder, create a text file called **`usernames.txt`** with one name per line:
   ```
   wumpus
   nelly
   some_rare.name
   ```
4. Double-click `dcheck.exe` and choose **[1]**.
5. When it finishes, the free names are in **`results\available.txt`** (next to the exe).

You can also drag any `.txt` file into the window when it asks, or paste names straight in.
Finish with an empty line.

> **Windows SmartScreen warning:** the exe is not code-signed, so Windows may show
> "Windows protected your PC". Click **More info**, then **Run anyway**.

## Finding free 2c / 3c / 4c names

From the menu, choose **[2]**, **[3]** or **[4]** to check every 2-, 3- or 4-character name.
It then asks whether to include `_` and `.` as well as letters and digits. From a terminal:

```
dcheck --gen 3                   # every 3-character name, a-z0-9
dcheck --gen 4 --charset all     # 4 characters, including _ and .
dcheck --gen 4 --limit 5000      # only check 5000 (random) 4c names
```

- `--charset`: `alnum` (a-z0-9, the default), `letters` (a-z), `digits` (0-9) or `all` (a-z0-9 `_` `.`).
- Results go to `results/<N>c-<charset>/`, for example `results/3c-alnum/available.txt`.
- **It resumes automatically.** Run the same thing again and it carries on where it stopped.
  Use `--fresh` to start over.
- **Ctrl+C is safe.** Every result is saved the moment it arrives.

How long it takes, at the default 0.5 s between requests (this is a minimum; rate limits make it slower):

| Length | `alnum` (a-z0-9) | `all` (a-z0-9 `_` `.`, no `..`) |
|-------:|------------------|---------------------------------|
| 2c | 1,296 names, about 11 min | 1,443 names, about 12 min |
| 3c | 46,656 names, about 6.5 h | 54,797 names, about 7.6 h |
| 4c | 1,679,616 names, about 9.7 days | 2,080,880 names, about 12 days |

**Be realistic:** almost every 2c and 3c name has been taken for a long time, so expect very
few hits or none. 4c has more free names, but checking all of them takes days. Use `--limit`
to do it in chunks. 4c names are always checked in random order (use `--shuffle` to get the
same for 2c and 3c), so a partial run samples the whole range instead of only names starting
with `aa`.

## Running from source (Python 3.10+)

```
python -m dcheck usernames.txt        # check a list
type names.txt | python -m dcheck     # or pipe names in (cat on macOS/Linux)
python -m dcheck --gen 3              # 3c finder
python -m dcheck                      # interactive menu
```

Run `pip install .` in this folder to get a `dcheck` command you can use instead of `python -m dcheck`.
List files may be separated by newlines, commas or spaces. Lines starting with `#` are
ignored. A leading `@` is removed and names are lowercased.

## All options

| Option | What it does |
|--------|--------------|
| `FILE ...` | Files with usernames. `-` reads stdin. With nothing given, `usernames.txt` is used (or the menu is shown) |
| `--gen N [N ...]` | Check every N-character name (2, 3 and/or 4). Output goes to `results/<N>c-<charset>/` and resumes automatically |
| `--charset {all,alnum,digits,letters}` | Characters for `--gen` (default: `alnum`) |
| `--shuffle` | Check generated names in random order (always on for 4c) |
| `--fresh` | With `--gen`: ignore earlier progress and start over |
| `--limit N` | Stop after checking N names |
| `-w`, `--workers N` | Number of concurrent worker threads (default: 3) |
| `--delay SECONDS` | Minimum seconds between requests across all workers (default: 0.5) |
| `--timeout SECONDS` | HTTP request timeout (default: 10) |
| `-o`, `--out DIR` | Output folder for the result files (default: `results`) |
| `--resume` | Skip names already in the output folder's result files |
| `--no-save` | Don't write the `.txt` result files |
| `--json FILE` | Also write all results as JSON to FILE |
| `--csv FILE` | Also write all results as CSV to FILE |
| `--only-available` | Only print available usernames to the console |
| `--no-color` | Disable colors (the `NO_COLOR` environment variable works too) |
| `--no-interactive` | Never show the menu. Exit with an error when no input is given |
| `-h`, `--help` | Show help and exit |

## How it works

dcheck uses the same check as Discord's sign-up page, which tells you whether a username
is taken:

```
POST https://discord.com/api/v9/unique-username/username-attempt-unauthed
{"username": "wumpus"}   ->   {"taken": true}  or  {"taken": false}
```

You don't need a login, account or token.

First, every name is checked against Discord's rules on your own computer: 2–32 characters,
only `a-z`, `0-9`, `_` and `.`, and no `..`. Names that break the rules are never sent to
Discord.

Each name ends up in one file in the output folder:

| Result | File | Meaning |
|--------|------|---------|
| available | `available.txt` | Not currently taken |
| taken | `taken.txt` | Someone already has it |
| invalid | `invalid.txt` | Breaks the naming rules (found locally, or rejected by Discord) |
| error | `errors.txt` | Couldn't be checked (network problem, kept being rate-limited, blocked). These are retried on the next run |

A file is only created if at least one name lands in it. A plain list check (not `--gen`)
starts these files fresh each time, unless you pass `--resume`.

## Limits & notes

- **Discord rate-limits this check.** When Discord says "slow down" (HTTP 429 with
  `retry_after`), every worker waits that long. Requests are also spaced out (`--delay`).
  Big lists are slow on purpose. **Don't set `--delay` to 0**: you'll only get rate-limited
  or blocked.
- **"Available" only means "not taken right now".** Discord may still refuse some names when
  you actually try to claim them (reserved or blocked words). Someone else can also take a
  name between your check and your claim.
- dcheck is not affiliated with or endorsed by Discord. Use it responsibly and within
  [Discord's Terms of Service](https://discord.com/terms).

## Development

```
pip install pytest && python -m pytest -q
```
