# STLoader
Python-based script for downloading lego-compatible brick STLs from set part lists. 

## How it works
- **Source:** the official LDraw parts library (`https://library.ldraw.org/library/updates/complete.zip`), downloaded once and cached in `%LOCALAPPDATA%\LEGO STL Downloader`. The unofficial snapshot (`ldrawunf.zip`) is fetched only when a part isn't in the official library.
- **Part lists:** You can use brick list .`csv` files from the following sources/with the following columns: Brickset (DesignID / Qty / Colour), Rebrickable (Part / Color / Quantity / Is Spare), BrickLink Studio (LdrawId / ColorName / Qty), BrickLink (Item No).
- **Part matching:**
  - Exact LDraw file first.
  - "~Moved" or outdated bricks use the newest version (3794 → 3794b).
  - Aliases are followed (30057 → 6141).
  - Decorated parts are ignored, you get the original instead (3626cpr0001 → 3626c).
  - Assemblies are split into printable pieces (73587 → 73587a: base + lever).
- **Output:**
  - One folder per colour. files are named `<part> - <LDraw name> (x<qty>).stl`.
  - `parts_report.csv` lists the match and notes for every part.
  - `README.txt` holds the CC BY attribution.

## Status
I tested this on a few sets (20010-1). This worked great, but if you have any issues feel free to open one!

