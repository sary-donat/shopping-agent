"""Download the chains' public price files and load them into SQLite.

    python -m data.prices download             # store lists + full price files for the stores in CITY
    python -m data.prices download RAMI_LEVY   # only the chains named
    python -m data.prices build     # parse the downloaded files into shopping.db
"""
import asyncio
import gzip
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from config import CHAINS, CITY, DUMPS_DIR
from data import db

if sys.platform == "win32" and "fcntl" not in sys.modules:
    # The download library locks its cache file with fcntl, which exists only on Linux/macOS.
    # We run a single download process, so the lock is not needed: give it a stand-in that does nothing.
    import types
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_EX=2, LOCK_UN=8, flock=lambda *args: None)

# ---------- download ----------

def chains() -> list[str]:
    """CHAINS from config, or - when it is empty - every chain the download library knows."""
    if CHAINS:
        return CHAINS
    from il_supermarket_scarper.scrappers_factory import ScraperFactory
    return [member.name for member in ScraperFactory]


def _scraper(chain: str):
    """The library's scraper for this chain. Taken straight from the enum, so chains the library
    marks as "not stable" are still tried (it would hide them)."""
    from il_supermarket_scarper.scrappers_factory import ScraperFactory
    from il_supermarket_scarper.utils.file_output import DiskFileOutput

    if chain not in ScraperFactory.__members__:
        raise ValueError(f"the download library has no chain called {chain}")
    return ScraperFactory[chain].value(file_output=DiskFileOutput(str(DUMPS_DIR / chain)))


async def _fetch(chain: str, file_type: str, store_id: int | None = None) -> None:
    async for _ in _scraper(chain).scrape(files_types=[file_type], store_id=store_id, limit=1):
        pass


# Chains on the Cerberus server (publishedprices.co.il). The download library reaches it over FTP,
# which fails there ("425 Unable to open the data connection"), so we use the site's HTTPS interface.
CERBERUS_URL = "https://url.publishedprices.co.il"
_CSRF = re.compile(r'name="csrftoken"\s+content="([^"]+)"')


def _cerberus_user(chain: str) -> str | None:
    """The chain's user name on the Cerberus server, or None if the chain lives elsewhere."""
    from il_supermarket_scarper.engines import Cerberus as LibraryCerberus
    scraper = _scraper(chain)
    return scraper.ftp_username if isinstance(scraper, LibraryCerberus) else None


class Cerberus:
    def __init__(self, chain: str, user: str):
        import requests
        self.chain = chain
        self.session = requests.Session()
        token = self._token("/login")
        self.session.post(f"{CERBERUS_URL}/login/user", timeout=30,
                          data={"username": user, "password": "", "csrftoken": token})
        self.names = self._list()

    def _token(self, page: str) -> str:
        html = self.session.get(CERBERUS_URL + page, timeout=30).text
        match = _CSRF.search(html)
        if not match:
            raise RuntimeError(f"no csrf token on {page} - the site may have changed or the login failed")
        return match.group(1)

    def _list(self) -> list[str]:
        r = self.session.post(f"{CERBERUS_URL}/file/json/dir", timeout=120,
                              data={"iDisplayLength": 100000, "sSearch": "", "cd": "/",
                                    "csrftoken": self._token("/file")})
        r.raise_for_status()
        return [row.get("name", "") for row in r.json().get("aaData", [])]

    def latest(self, prefix: str, store_id: int | None = None) -> str | None:
        """Newest file starting with prefix (names end with the date, so the max name is the newest)."""
        found = [n for n in self.names if n.lower().startswith(prefix.lower())
                 and (store_id is None or _store_id_from_filename(n) == store_id)]
        return max(found, key=str.lower) if found else None

    def save(self, name: str) -> None:
        r = self.session.get(f"{CERBERUS_URL}/file/d/{name}", timeout=300)
        r.raise_for_status()
        folder = DUMPS_DIR / self.chain
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name.rsplit(".", 1)[0] + ".xml")).write_bytes(_unpack(r.content))


def _download_cerberus(chain: str, user: str) -> None:
    site = Cerberus(chain, user)
    print(f"   {len(site.names)} files on the site")
    stores_file = site.latest("stores")
    if not stores_file:
        print("   no store list on the site")
        return
    for old in _files(chain, "store"):
        old.unlink()  # keep only the newest store list, so stores are not counted twice
    site.save(stores_file)
    for store in _city_stores(chain):
        name = site.latest("pricefull", store["store_id"])
        print(f"   prices for store {store['store_id']} {store['name']}: {name or 'no PriceFull file'}")
        if name:
            site.save(name)


def _city_stores(chain: str) -> list[dict]:
    all_stores = _stores_of(chain)
    city_stores = [s for s in all_stores if _in_city(s)]
    print(f"   stores in {CITY}: {len(city_stores)} of {len(all_stores)}")
    if all_stores and not city_stores:
        print("   no match - check how this chain writes the city:", all_stores[0])
    return city_stores


def download(selected: list[str] | None = None) -> None:
    summary = {}
    for chain in selected or chains():
        print(f"\n### {chain}: store list")
        try:
            user = _cerberus_user(chain)
            if user:
                _download_cerberus(chain, user)
            else:
                _download_library(chain)
        except Exception as e:  # noqa: BLE001 - one broken chain must not stop the others
            print(f"   failed: {e}")
        n_city = len([s for s in _stores_of(chain) if _in_city(s)])
        n_prices = len(_files(chain, "pricefull"))
        summary[chain] = (len(_stores_of(chain)), n_city, n_prices)
    print(f"\n### summary - stores in the chain / in {CITY} / price files")
    for chain, (total, city, prices) in sorted(summary.items(), key=lambda kv: -kv[1][1]):
        note = "no store list (site down or empty)" if total == 0 else ""
        print(f"   {chain:30} {total:5} {city:4} {prices:4}  {note}")


def _download_library(chain: str) -> None:
    asyncio.run(_fetch(chain, "STORE_FILE"))
    if not _files(chain, "store"):
        print("   no store list was saved - the chain's site gave no files (see logging.log)")
        return
    for store in _city_stores(chain):
        print(f"   prices for store {store['store_id']} {store['name']}")
        try:
            asyncio.run(_fetch(chain, "PRICE_FULL_FILE", store["store_id"]))
        except Exception as e:  # noqa: BLE001
            print(f"   failed: {e}")

# ---------- parse ----------

def _unpack(data: bytes) -> bytes:
    """Files arrive as plain XML, gzip, or zip (some zip files are even named .gz)."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.read(z.namelist()[0])
    return data


def _read_xml(path: Path) -> bytes:
    return _unpack(path.read_bytes())


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _records(path: Path, key_field: str):
    """Every XML element that has a <key_field> child, as {lowercase_tag: text}."""
    root = ET.fromstring(_read_xml(path))
    for el in root.iter():
        rec = {_local(c.tag): (c.text or "").strip() for c in el}
        if key_field in rec:
            yield rec


def _files(chain: str, kind: str) -> list[Path]:
    # Some chains' files are saved with no extension (still gzip-compressed) - accept any file but the library's json.
    folder = DUMPS_DIR / chain
    if not folder.exists():
        return []
    return [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() != ".json" and p.name.lower().startswith(kind)]


def _stores_of(chain: str) -> list[dict]:
    stores = {}  # by store id, so a store listed in an older and a newer file counts once
    for path in sorted(_files(chain, "store"), key=lambda p: p.name.lower()):
        try:
            records = list(_records(path, "storeid"))
        except ET.ParseError as e:
            print(f"   cannot read {path.name}: {e}")
            continue
        for r in records:
            if not r["storeid"].isdigit():
                continue
            stores[int(r["storeid"])] = {
                "store_id": int(r["storeid"]),
                "name": r.get("storename", ""),
                "address": r.get("address", ""),
                "city": r.get("city", ""),
            }
    return list(stores.values())


# Some chains (Shufersal, Hazi Hinam) write the city as the CBS locality code, not its name.
CITY_CODES = {
    "בני ברק": "6100", "תל אביב": "5000", "ירושלים": "3000", "חיפה": "4000",
    "פתח תקווה": "7900", "רמת גן": "8600", "ראשון לציון": "8300", "חולון": "6600",
    "נתניה": "7400", "אשדוד": "70", "באר שבע": "9000", "בת ים": "6200",
}


def _norm(text: str) -> str:
    return " ".join(text.replace("-", " ").split())


def _in_city(store: dict) -> bool:
    city = _norm(CITY)
    if CITY_CODES.get(city) and store["city"].strip().lstrip("0") == CITY_CODES[city]:
        return True
    return any(city in _norm(store[f]) for f in ("city", "address", "name"))


def _store_id_of(path: Path) -> int:
    # The file header holds <StoreId>; the file name is a fallback
    # (formats differ: PriceFull<chain>-012-<date> vs PriceFull<chain>-006-219-<date>).
    match = re.search(rb"<StoreI[dD]>\s*0*(\d+)\s*<", _read_xml(path)[:4000])
    return int(match.group(1)) if match else _store_id_from_filename(path.name)


def _store_id_from_filename(name: str) -> int:
    # the store is the last short number before the date: ...-012-<date> or ...-006-219-<date>
    groups = re.findall(r"-(\d{1,4})(?=-)", name)
    return int(groups[-1]) if groups else -1


def build() -> None:
    conn = db.connect()
    # remove the invented demo data (python -m data.demo), so it never mixes with real prices
    conn.execute("DELETE FROM stores WHERE chain LIKE 'DEMO_%'")
    conn.execute("DELETE FROM prices WHERE chain LIKE 'DEMO_%'")
    conn.execute("DELETE FROM items WHERE item_code NOT IN (SELECT item_code FROM prices)")
    for chain in chains():
        conn.execute("DELETE FROM stores WHERE chain = ?", (chain,))
        conn.execute("DELETE FROM prices WHERE chain = ?", (chain,))
        stores = [s for s in _stores_of(chain) if _in_city(s)]
        conn.executemany(
            "INSERT OR REPLACE INTO stores (chain, store_id, name, address, city) VALUES (?, ?, ?, ?, ?)",
            [(chain, s["store_id"], s["name"], s["address"], s["city"]) for s in stores],
        )
        n_prices = 0
        # oldest file first, so when a store has several, the newest price is the one kept
        for path in sorted(_files(chain, "pricefull"), key=lambda p: p.name.lower()):
            store_id = _store_id_of(path)
            try:
                records = list(_records(path, "itemcode"))
            except ET.ParseError as e:
                print(f"   cannot read {path.name}: {e}")
                continue
            for r in records:
                code = r["itemcode"]
                if len(code) < 8 or not re.fullmatch(r"\d+(\.\d+)?", r.get("itemprice", "")):
                    continue  # short codes are chain-internal, not comparable between chains
                name = r.get("itemname") or r.get("itemnm", "")
                conn.execute(
                    "INSERT OR IGNORE INTO items VALUES (?, ?, ?, ?, ?)",
                    (code, name, r.get("manufacturername") or r.get("manufacturename", ""),
                     r.get("quantity", ""), r.get("unitqty") or r.get("unitofmeasure", "")),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?)",
                    (chain, store_id, code, float(r["itemprice"]), r.get("priceupdatedate", "")),
                )
                n_prices += 1
        print(f"{chain}: {len(stores)} stores in {CITY}, {n_prices} prices")
    conn.execute("DELETE FROM items_fts")
    conn.execute("INSERT INTO items_fts SELECT item_code, name, manufacturer FROM items")
    conn.commit()
    print("Database ready:", conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], "products")
    print("Next: python -m data.geo stores   (adds map coordinates to the stores)")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "download":
        download(sys.argv[2:] or None)
    elif sys.argv[1:] == ["build"]:
        build()
    else:
        sys.exit(__doc__)
