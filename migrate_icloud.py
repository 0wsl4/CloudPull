#!/usr/bin/env python3
"""Migrador de albumes de iCloud Fotos a una carpeta local (y opcionalmente MEGA).

Uso:
  python migrate_icloud.py --setup          # pregunta cuenta de iCloud y carpeta de destino
  python migrate_icloud.py --login          # abre una ventana para iniciar sesion en iCloud
  python migrate_icloud.py --inventory      # lista los albumes de iCloud
  python migrate_icloud.py --pending        # albumes aun no descargados
  python migrate_icloud.py --all            # descarga todos los pendientes
  python migrate_icloud.py --album "Nombre" # descarga un album concreto
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
INVENTORY_PATH = PROJECT_DIR / "inventory.json"
PENDING_PATH = PROJECT_DIR / "pending.json"
FAILURES_LOG = PROJECT_DIR / "logs" / "fallos_albumes.log"
ICLOUD_ALBUMS_URL = "https://www.icloud.com/photos/#/albums/"
HOME = Path.home()
DOWNLOADS_DIR = HOME / "Downloads"
if sys.platform == "win32":
    STATE_BASE = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    MEGA_CACHE_DIR = STATE_BASE / "Mega Limited" / "MegAsync"
else:
    STATE_BASE = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share"))
    MEGA_CACHE_DIR = STATE_BASE / "data" / "Mega Limited" / "MEGAsync"
STATE_DIR = STATE_BASE / "iCloudMigrator"

DEFAULT_CONFIG: dict[str, Any] = {
    "apple_id": "",
    "destination": "",
    "verify_megasync": False,
    "skip_folders": [],
}
CONFIG: dict[str, Any] = dict(DEFAULT_CONFIG)


def load_config() -> None:
    if CONFIG_PATH.exists():
        CONFIG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def require_config() -> None:
    if not CONFIG.get("destination"):
        raise SystemExit("Falta la configuracion. Ejecuta primero: python migrate_icloud.py --setup")


def albums_dir() -> Path:
    return Path(CONFIG["destination"]).expanduser()


def profile_dir() -> Path:
    account = re.sub(r"[^A-Za-z0-9._-]+", "_", CONFIG.get("apple_id") or "default")
    return STATE_DIR / "profiles" / account


CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)


def log(message: str) -> None:
    print(message, flush=True)


class TeeStream:
    """Write process output to both the console and the session log."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)

    def fileno(self):
        return self.streams[0].fileno()


def start_session_log():
    log_path = os.environ.get("ICLOUD_MIGRATOR_LOG")
    if not log_path:
        return None
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(sys.__stdout__, handle)
    sys.stderr = TeeStream(sys.__stderr__, handle)
    print(f"\n===== Inicio {datetime.datetime.now().astimezone().isoformat(timespec='seconds')} =====")
    print("Comando:", " ".join(sys.argv))
    return handle


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def launch(headless: bool = True):
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="chrome",
        headless=headless,
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": 1600, "height": 1000},
        accept_downloads=True,
        user_agent=CHROME_UA,
        args=[
            "--no-first-run",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-features=RestoreSessionState",
            "--disable-blink-features=AutomationControlled",
            "--window-position=80,80",
            "--window-size=1500,950",
        ],
    )
    return playwright, context


def page_of(context):
    page = context.pages[0] if context.pages else context.new_page()
    return page


def photos_frame(page):
    for frame in page.frames:
        if "/applications/photos3/" in frame.url:
            return frame
    raise RuntimeError("No se encontro el marco de iCloud Fotos")


def is_logged_in(page) -> bool:
    try:
        title = page.title()
    except Exception:
        title = ""
    if re.match(r"^\s*[ÁáA]lbumes\s*-", title):
        return True
    try:
        frame = photos_frame(page)
        body = frame.locator("body").inner_text(timeout=8000)
    except PlaywrightTimeoutError:
        return False
    except RuntimeError:
        return False
    if "Iniciar sesi" in body:
        return False
    return bool(re.search(r"\b\d+\s+[aá]lbumes\b", body)) and "/photos/#/albums" in page.url


def wait_for_albums_page(page, timeout_ms: int = 90000) -> None:
    page.goto(ICLOUD_ALBUMS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if is_logged_in(page):
            try:
                photos_frame(page).get_by_text(re.compile(r"\b\d+\s+[aá]lbumes\b", re.I)).first.wait_for(
                    state="visible", timeout=5000
                )
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(3000)
            return
        page.wait_for_timeout(2500)
    raise RuntimeError(
        "iCloud no muestra la sesion iniciada. Ejecuta primero: python migrate_icloud.py --login"
    )


def collect_albums(page) -> list[dict[str, Any]]:
    script = r"""
    () => {
      const out = [];
      const all = document.querySelectorAll('.grid-item');
      for (const el of all) {
        const text = (el.innerText || '').trim();
        if (!text || text.length > 300) continue;
        const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
        const countLine = lines.find(line => /^\d[\d.,]*\s+elementos?$/i.test(line) || /^Ningún elemento$/i.test(line));
        if (!countLine) continue;
        const name = lines.find(line => line !== countLine);
        const countMatch = countLine.match(/^([\d.,]+)/);
        const count = countMatch ? parseInt(countMatch[1].replace(/[.,]/g, ''), 10) : 0;
        if (!name || !Number.isFinite(count)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 40 || rect.height < 30) continue;
        let href = el.getAttribute('href') || '';
        if (!href) {
          const a = el.closest('a');
          if (a) href = a.getAttribute('href') || '';
        }
        out.push({name, count, href, area: rect.width * rect.height});
      }
      const best = new Map();
      for (const item of out) {
        const prev = best.get(item.name);
        if (!prev || item.area < prev.area) best.set(item.name, item);
      }
      return Array.from(best.values()).map(({name, count, href}) => ({name, count, href}));
    }
    """
    albums: dict[str, dict[str, Any]] = {}
    frame = photos_frame(page)
    frame.evaluate("""() => { const scroller=document.querySelector('.grid-scroll'); if(scroller) scroller.scrollTop=0; }""")
    page.wait_for_timeout(1000)
    stagnant = 0
    for _ in range(120):
        found = frame.evaluate(script)
        before = len(albums)
        for item in found:
            albums.setdefault(item["name"], item)
        if len(albums) == before:
            stagnant += 1
        else:
            stagnant = 0
        scrolled = frame.evaluate(
            r"""
            () => {
              const els = Array.from(document.querySelectorAll('*'));
              let best = null;
              for (const el of els) {
                const style = getComputedStyle(el);
                if (!/(auto|scroll)/.test(style.overflowY)) continue;
                const delta = el.scrollHeight - el.clientHeight;
                if (delta < 200) continue;
                if (!best || delta > best.delta) best = {el, delta};
              }
              if (best) {
                const before = best.el.scrollTop;
                best.el.scrollTop = Math.min(
                  best.el.scrollHeight,
                  best.el.scrollTop + Math.max(best.el.clientHeight * 0.9, 600)
                );
                return best.el.scrollTop !== before;
              }
              const before = window.scrollY;
              window.scrollBy(0, 900);
              return window.scrollY !== before;
            }
            """
        )
        page.wait_for_timeout(1200)
        if not scrolled and stagnant >= 2:
            break
        if stagnant >= 6:
            break
    return sorted(albums.values(), key=lambda item: item["name"].casefold())


def open_album(page, album: str) -> None:
    frame = photos_frame(page)
    frame.evaluate("""() => { const scroller=document.querySelector('.grid-scroll'); if(scroller) scroller.scrollTop=0; }""")
    page.wait_for_timeout(700)
    card = None
    for _ in range(80):
        # Coincidencia exacta de la linea del nombre: "Viaje" no debe casar con "Viaje 2024".
        exact_count = frame.evaluate(
            """(name) => {
              document.querySelectorAll('[data-mig-target]').forEach(el => el.removeAttribute('data-mig-target'));
              let n = 0;
              for (const el of document.querySelectorAll('.grid-item')) {
                const lines = (el.innerText || '').trim().split('\\n').map(s => s.trim());
                if (lines.includes(name)) { el.setAttribute('data-mig-target', '1'); n++; }
              }
              return n;
            }""",
            album,
        )
        if exact_count:
            if exact_count != 1:
                raise RuntimeError(f"Nombre ambiguo o tarjeta duplicada en iCloud: {album}")
            card = frame.locator(".grid-item[data-mig-target]").first
            break
        moved = frame.evaluate(
            """() => { const s=document.querySelector('.grid-scroll'); if(!s) return false;
            const before=s.scrollTop; s.scrollTop=Math.min(s.scrollHeight,
            before+Math.max(s.clientHeight*0.85,600)); return s.scrollTop!==before; }"""
        )
        if not moved:
            break
        page.wait_for_timeout(700)
    if card is None:
        raise RuntimeError(f"No se encontro el album en la rejilla: {album}")
    card.dblclick()
    page.wait_for_url(re.compile(r"#/albums/a,"), timeout=30000)
    page.wait_for_timeout(1000)


VISIBLE_CARDS_JS = r"""
() => {
  const sc = document.querySelector('.grid-scroll');
  const r = sc.getBoundingClientRect();
  const out = [];
  for (const el of document.querySelectorAll('.grid-item')) {
    const t = (el.getAttribute('style') || '').match(/translateX\(([-\d.]+)px\) translateY\(([-\d.]+)px\)/);
    if (!t) continue;
    const b = el.getBoundingClientRect();
    out.push({x: t[1], y: t[2], visible: b.top >= r.top && b.bottom <= r.bottom});
  }
  return {cards: out, top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight, step: sc.clientHeight * 0.7};
}
"""


def select_virtualized(page, frame, expected: int) -> None:
    """Select every card of a virtualized grid one by one (first plain click, then Ctrl+click)."""
    frame.evaluate("() => { document.querySelector('.grid-scroll').scrollTop = 0; }")
    page.wait_for_timeout(800)
    selected: set[tuple[str, str]] = set()
    stagnant = 0
    for _ in range(400):
        state = frame.evaluate(VISIBLE_CARDS_JS)
        for card in state["cards"]:
            key = (card["x"], card["y"])
            if key in selected or not card["visible"]:
                continue
            target = frame.locator(
                f".grid-item[style*='translateX({card['x']}px) translateY({card['y']}px)']"
            )
            if target.count() != 1:
                continue
            if selected:
                target.first.click(modifiers=["Control"], timeout=5000)
            else:
                target.first.click(timeout=5000)
            selected.add(key)
            page.wait_for_timeout(350)
        if len(selected) >= expected:
            break
        if state["top"] >= state["max"] - 1:
            stagnant += 1
            if stagnant >= 2:
                break
        frame.evaluate(
            "(step) => { const s = document.querySelector('.grid-scroll'); s.scrollTop += step; }",
            state["step"],
        )
        page.wait_for_timeout(700)
    log(f"Seleccion virtualizada: {len(selected)} tarjetas pulsadas de {expected} esperadas")
    if len(selected) != expected:
        raise RuntimeError(
            f"Se pulsaron {len(selected)} tarjetas y el inventario espera {expected}; no se inicio la descarga"
        )


def click_download(page, album: str) -> None:
    frame = photos_frame(page)
    inventory = load_inventory()
    expected = next((int(item["count"]) for item in inventory if item["name"] == album), None)
    items = frame.locator(".grid-item")
    count = items.count()
    if expected is None:
        raise RuntimeError("No se encontro el conteo del album en inventory.json")
    if count == expected:
        for index in range(count):
            if index == 0:
                items.nth(index).click()
            else:
                items.nth(index).click(modifiers=["Control"])
            page.wait_for_timeout(350)
    elif count < expected:
        # Rejilla virtualizada: solo hay ~90 tarjetas montadas. Se selecciona una a una
        # mientras se desplaza; la posicion absoluta de cada tarjeta es su identificador.
        select_virtualized(page, frame, expected)
        items = frame.locator(".grid-item")
    else:
        raise RuntimeError(f"Elementos montados en iCloud ({count}) > inventario ({expected}); no se selecciono nada")

    card_state = items.evaluate_all(
        """els => els.map(el => ({
          text: (el.innerText || '').trim().replace(/\\s+/g, ' '),
          class: el.className,
          ariaSelected: el.getAttribute('aria-selected'),
          ariaPressed: el.getAttribute('aria-pressed'),
          dataState: el.getAttribute('data-state')
        }))"""
    )
    log("Estado de tarjetas tras clics: " + json.dumps(card_state, ensure_ascii=False))

    more = frame.locator("ui-button.GridMoreButton")
    if not more.count() or not more.first.is_visible():
        raise RuntimeError("No se encontro el menu de tres puntos del album")
    more.first.click(timeout=2000)
    menu_download = frame.locator("ui-menu-item[role='menuitem']").filter(
        has_text=re.compile(r"^Descargar", re.I)
    )
    if not menu_download.count() or not menu_download.first.is_visible():
        raise RuntimeError("No se encontro Descargar en el menu de tres puntos")
    menu_label = menu_download.first.inner_text().strip()
    selected_match = re.search(r"Descargar\s+(\d+)\s+elementos?", menu_label, re.I)
    if selected_match and int(selected_match.group(1)) != expected:
        raise RuntimeError(
            f"iCloud indica {selected_match.group(1)} elementos seleccionados; "
            f"el inventario espera {expected}. No se inicio la descarga"
        )
    menu_download.first.click(timeout=2000)
    page.wait_for_timeout(1500)
    # A toolbar button with the same accessible name remains visible after the
    # menu action. Only confirm inside an actual dialog; clicking the toolbar
    # control here can trigger a second, identical download.
    dialogs = frame.locator('[role="dialog"], ui-dialog')
    if not dialogs.count() or not dialogs.first.is_visible():
        return
    for label in ("Descargar", "Descargar de todos modos"):
        try:
            dialog_button = dialogs.first.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
            if dialog_button.count() and dialog_button.first.is_visible():
                dialog_button.first.click(timeout=1500)
                break
        except Exception:
            continue


def download_album(page, album: str) -> list[Path]:
    """Collect either one ZIP or all individual downloads started by iCloud."""
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    click_download(page, album)

    deadline = time.time() + 1800
    while not downloads and time.time() < deadline:
        page.wait_for_timeout(250)
    if not downloads:
        raise RuntimeError("iCloud no inicio ninguna descarga")

    # iCloud may emit one download event per selected item. Wait for a quiet
    # interval before closing the page so later items are not lost.
    quiet_since = time.time()
    observed = 0
    while time.time() < deadline:
        page.wait_for_timeout(250)
        if len(downloads) != observed:
            observed = len(downloads)
            quiet_since = time.time()
        elif time.time() - quiet_since >= 10:
            break
    stamp = time.strftime("%Y%m%d_%H%M%S")
    staging = DOWNLOADS_DIR / f"iCloud_{re.sub(r'[^A-Za-z0-9._ -]+', '_', album).strip()}_{stamp}_files"
    staging.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, download in enumerate(downloads, start=1):
        suggested = Path(download.suggested_filename or f"archivo_{index}").name
        path = staging / suggested
        if path.exists():
            path = staging / f"{Path(suggested).stem}_{index}{Path(suggested).suffix}"
        download.save_as(str(path))
        log(f"Descarga recibida: {suggested} ({path.stat().st_size / 1048576:.1f} MB)")
        paths.append(path)
    return paths


def validate_zip(zip_path: Path) -> tuple[int, int, str | None]:
    with zipfile.ZipFile(zip_path) as archive:
        bad = archive.testzip()
        entries = [info for info in archive.infolist() if not info.is_dir()]
        total = sum(info.file_size for info in entries)
        roots = {PurePosixPath(info.filename).parts[0] for info in entries if PurePosixPath(info.filename).parts}
        root = next(iter(roots)) if len(roots) == 1 else None
        return len(entries), total, bad if bad else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zip(zip_path: Path, destination: Path, allow_existing: bool = False) -> tuple[int, int]:
    if not allow_existing and destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"Destino no vacio: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = 0
    skipped = 0
    with zipfile.ZipFile(zip_path) as archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        roots = {PurePosixPath(info.filename).parts[0] for info in entries if PurePosixPath(info.filename).parts}
        strip_root = len(roots) == 1
        for info in entries:
            parts = PurePosixPath(info.filename).parts
            if strip_root:
                parts = parts[1:]
            if not parts or any(part in ("..", "") for part in parts):
                skipped += 1
                continue
            out_path = destination.joinpath(*parts)
            if out_path.exists():
                raise RuntimeError(f"No se sobrescribe un archivo extraido: {out_path}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(out_path, "wb") as target_file:
                shutil.copyfileobj(source, target_file, length=1024 * 1024)
            extracted += 1
            total += info.file_size
    log(f"Extraidos: {extracted} archivos, {total / 1048576:.1f} MB, omitidos: {skipped}")
    return extracted, total


def mega_state_db() -> Path:
    candidates = [
        path
        for path in MEGA_CACHE_DIR.glob("megaclient_statecache15_*.db")
        if all(word not in path.name for word in ("transfers", "prefs", "status", "05QC"))
    ]
    if not candidates:
        raise RuntimeError(f"No se encontro la cache de MEGAsync en {MEGA_CACHE_DIR}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def mega_album_folders() -> dict[str, list[dict[str, Any]]]:
    database = mega_state_db()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        result: dict[str, list[dict[str, Any]]] = {}
        for handle, parent, name in connection.execute(
            "select nodehandle, parenthandle, name from nodes where type=1"
        ):
            result.setdefault(normalize(name), []).append(
                {"handle": handle, "parent": parent, "name": name}
            )
        return result
    finally:
        connection.close()


def mega_children(folder_handle: int) -> list[tuple[str, int]]:
    database = mega_state_db()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return connection.execute(
            "select name, type from nodes where parenthandle=?", (folder_handle,)
        ).fetchall()
    finally:
        connection.close()


def verify_mega_album(album: str, local_dir: Path) -> bool:
    database = mega_state_db()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        folders = connection.execute(
            "select nodehandle, parenthandle from nodes where name=? and type=1", (album,)
        ).fetchall()
        if not folders:
            log(f"No se encontro la carpeta remota: {album}")
            return False
        local = {entry.name for entry in os.scandir(local_dir) if entry.is_file()}
        log(f"Archivos locales: {len(local)}")
        ok = True
        for handle, parent in folders:
            remote = {
                row[0]
                for row in connection.execute(
                    "select name from nodes where parenthandle=?", (handle,)
                )
            }
            missing = local - remote
            extra = remote - local
            log(
                f"Carpeta remota {handle} (padre {parent}): "
                f"remotos={len(remote)} faltan_en_mega={len(missing)} sobran_en_mega={len(extra)}"
            )
            if missing:
                log(f"  Faltan: {sorted(missing)[:10]}")
            if extra:
                log(f"  Sobran: {sorted(extra)[:10]}")
            ok = ok and not missing and not extra
        return ok
    finally:
        connection.close()


def load_inventory() -> list[dict[str, Any]]:
    if not INVENTORY_PATH.exists():
        raise RuntimeError(
            f"No existe {INVENTORY_PATH.name}. Ejecuta primero: python migrate_icloud.py --inventory"
        )
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("albums", [])
    return data


def album_status() -> dict[str, Any]:
    """Albumes ya descargados: subcarpetas del destino y de las carpetas extra indicadas en skip_folders."""
    root = albums_dir()
    done = {entry.name for entry in root.iterdir() if entry.is_dir()} if root.exists() else set()
    if CONFIG.get("verify_megasync"):
        folders = mega_album_folders()
        for item in folders.get(normalize(root.name), []):
            done |= {name for name, kind in mega_children(item["handle"]) if kind == 1}
    extra: set[str] = set()
    for folder in CONFIG.get("skip_folders", []):
        path = Path(folder).expanduser()
        if path.exists():
            extra |= {entry.name for entry in path.iterdir() if entry.is_dir()}
    return {
        "albums": sorted(done, key=str.casefold),
        "albums_norm": {normalize(name): name for name in done},
        "fotos": sorted(extra, key=str.casefold),
        "fotos_norm": {normalize(name): name for name in extra},
    }


def print_pending() -> None:
    status = album_status()
    inventory = load_inventory()
    pending: list[dict[str, Any]] = []
    already_albums: list[str] = []
    already_fotos: list[str] = []
    for item in inventory:
        key = normalize(item["name"])
        if key in status["albums_norm"]:
            already_albums.append(item["name"])
        elif key in status["fotos_norm"]:
            already_fotos.append(item["name"])
        else:
            pending.append(item)

    log(f"Albumes en iCloud: {len(inventory)}")
    log(f"Ya en el destino: {len(already_albums)}")
    log(f"Ya presentes en carpetas extra (skip_folders): {len(already_fotos)}")
    log(f"Pendientes reales: {len(pending)}")
    if pending:
        log("\nPendientes:")
        for item in pending:
            log(f"  - {item['name']} ({item.get('count', '?')} elementos)")
    if already_fotos:
        log("\nYa presentes en carpetas extra:")
        for name in already_fotos:
            log(f"  - {name}")
    PENDING_PATH.write_text(
        json.dumps(
            {
                "inventory_count": len(inventory),
                "albums": already_albums,
                "fotos": already_fotos,
                "pending": pending,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def command_choose_album(args) -> int:
    status = album_status()
    pending = [
        item
        for item in load_inventory()
        if normalize(item["name"]) not in status["albums_norm"]
        and normalize(item["name"]) not in status["fotos_norm"]
    ]
    if not pending:
        log("No hay albumes pendientes segun el inventario actual.")
        return 0

    log(f"Albumes pendientes ({len(pending)}):")
    for index, item in enumerate(pending, start=1):
        log(f"{index}. {item['name']} ({item.get('count', '?')} elementos)")
    selection = input("\nEscribe el numero de la lista o el nombre exacto del album: ").strip()
    if selection.isdigit():
        index = int(selection)
        if not 1 <= index <= len(pending):
            raise RuntimeError(f"Numero fuera de rango: {index}; elige entre 1 y {len(pending)}")
        args.album = pending[index - 1]["name"]
    else:
        matches = [item["name"] for item in pending if item["name"].casefold() == selection.casefold()]
        if len(matches) != 1:
            raise RuntimeError(f"No hay un album pendiente con el nombre exacto: {selection}")
        args.album = matches[0]
    log(f"Album elegido: {args.album}")
    return command_album(args)


def record_failure(album: str, reason: str) -> None:
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with FAILURES_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {album}: {reason}\n")


def command_all(args) -> int:
    status = album_status()
    pending = [
        item
        for item in load_inventory()
        if normalize(item["name"]) not in status["albums_norm"]
        and normalize(item["name"]) not in status["fotos_norm"]
    ]
    if not pending:
        log("No hay albumes pendientes segun el inventario actual.")
        return 0

    log(f"Migrando {len(pending)} albumes pendientes. Fallos en: {FAILURES_LOG}")
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    for index, item in enumerate(pending, start=1):
        name = item["name"]
        log(f"\n===== [{index}/{len(pending)}] {name} =====")
        if int(item.get("count", 0)) == 0:
            log("Album vacio en iCloud; se omite.")
            continue
        args.album = name
        reason = None
        try:
            code = command_album(args)
            if code:
                reason = f"codigo de salida {code} (verificacion remota o proceso incompleto)"
        except KeyboardInterrupt:
            raise
        except SystemExit as exc:
            reason = f"SystemExit: {exc.code}"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        if reason:
            log(f"FALLO en '{name}': {reason}. Se continua con el siguiente.")
            record_failure(name, reason)
            failed.append((name, reason))
        else:
            done.append(name)

    log(f"\nResumen: {len(done)} correctos, {len(failed)} con fallo, de {len(pending)} pendientes.")
    for name, reason in failed:
        log(f"  - {name}: {reason}")
    return 4 if failed else 0


def command_setup(args) -> int:
    """Pregunta la cuenta de iCloud y la carpeta de destino y guarda config.json."""
    print("Configuracion inicial (Enter conserva el valor actual).\n")
    apple_id = input(f"Cuenta de iCloud (correo) [{CONFIG['apple_id']}]: ").strip() or CONFIG["apple_id"]
    default_dest = CONFIG["destination"] or str(HOME / "Pictures" / "iCloud")
    destination = input(f"Carpeta donde guardar las fotos [{default_dest}]: ").strip() or default_dest
    answer = input("Sincronizas esa carpeta con MEGA y quieres verificar la subida? (s/N): ").strip().lower()
    CONFIG.update(
        apple_id=apple_id,
        destination=str(Path(destination).expanduser()),
        verify_megasync=answer in ("s", "si", "y", "yes"),
    )
    Path(CONFIG["destination"]).mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Configuracion guardada en {CONFIG_PATH}")
    log("Siguiente paso: python migrate_icloud.py --login")
    return 0


def command_login(args) -> int:
    """Abre una ventana de Chrome para que el usuario inicie sesion; la sesion queda en un perfil local."""
    playwright, context = launch(headless=False)
    try:
        page = page_of(context)
        page.goto(ICLOUD_ALBUMS_URL, wait_until="domcontentloaded", timeout=90000)
        who = CONFIG.get("apple_id") or "tu cuenta"
        log(f"Inicia sesion en iCloud con {who} en la ventana abierta (incluido el codigo de verificacion).")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if is_logged_in(page):
                page.wait_for_timeout(4000)
                log("Sesion detectada y guardada en el perfil local. Ya puedes cerrar esta ventana.")
                return 0
            page.wait_for_timeout(3000)
        log("No se detecto la sesion a tiempo. Vuelve a ejecutar --login.")
        return 2
    finally:
        context.close()
        playwright.stop()


def command_inventory(args) -> int:
    playwright, context = launch()
    try:
        page = page_of(context)
        wait_for_albums_page(page)
        albums = collect_albums(page)
        INVENTORY_PATH.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": ICLOUD_ALBUMS_URL,
                    "count": len(albums),
                    "albums": albums,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"Inventario guardado: {INVENTORY_PATH} ({len(albums)} albumes)")
        for item in albums[:10]:
            log(f"  - {item['name']} ({item['count']} elementos)")
        return 0
    finally:
        context.close()
        playwright.stop()


def command_pending(args) -> int:
    print_pending()
    return 0


def command_album(args) -> int:
    album = args.album.strip()
    if not album:
        raise SystemExit("Indica el nombre del album con --album")
    storage_album = album.rstrip(" .")
    if not storage_album:
        raise SystemExit("El nombre del album no es valido como nombre de carpeta en Windows")
    status = album_status()
    if normalize(album) in status["albums_norm"]:
        log(f"El album ya esta en el destino: {album}")
        return 0
    if normalize(album) in status["fotos_norm"]:
        log(f"El album ya esta en una carpeta extra: {album}")
        return 0
    destination = albums_dir() / storage_album
    if args.destination_root:
        destination = Path(args.destination_root).expanduser() / storage_album
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"El destino ya existe y no esta vacio: {destination}")

    playwright, context = launch()
    try:
        page = page_of(context)
        wait_for_albums_page(page)
        log(f"Abriendo album: {album}")
        open_album(page, album)
        log("Album abierto. Seleccionando los elementos y descargando desde el menu...")
        inventory = load_inventory()
        expected = next(int(item["count"]) for item in inventory if item["name"] == album)
        downloaded = download_album(page, album)
    finally:
        context.close()
        playwright.stop()

    zip_paths = [path for path in downloaded if zipfile.is_zipfile(path)]
    if zip_paths:
        if len(zip_paths) != len(downloaded):
            raise RuntimeError("iCloud mezclo ZIP y archivos directos; se conservan en Descargas")
        unique_paths = set()
        duplicated_paths = set()
        total_bytes = 0
        for zip_path in zip_paths:
            entries, size, bad = validate_zip(zip_path)
            if bad:
                raise SystemExit(f"ZIP corrupto ({zip_path.name}), primera entrada mala: {bad}")
            total_bytes += size
            with zipfile.ZipFile(zip_path) as archive:
                files_in_zip = [info for info in archive.infolist() if not info.is_dir()]
                roots = {PurePosixPath(info.filename).parts[0] for info in files_in_zip if PurePosixPath(info.filename).parts}
                strip_root = len(roots) == 1
                for info in files_in_zip:
                    parts = PurePosixPath(info.filename).parts
                    relative = PurePosixPath(*parts[1:] if strip_root else parts).as_posix()
                    key = relative.casefold()
                    if key in unique_paths:
                        duplicated_paths.add(relative)
                    unique_paths.add(key)
        if duplicated_paths:
            raise RuntimeError(
                "Los ZIP contienen rutas repetidas (" + ", ".join(sorted(duplicated_paths)) + "); "
                "se conservan para revision y no se copian al destino"
            )
        if len(unique_paths) < expected:
            raise RuntimeError(
                f"Los ZIP contienen {len(unique_paths)} archivos unicos para {expected} elementos; "
                "se conservan en Descargas y no se copian al destino"
            )
        log(f"ZIP validos: {len(zip_paths)} archivos, {len(unique_paths)} entradas unicas, {total_bytes / 1048576:.1f} MB")
        extracted = 0
        for index, zip_path in enumerate(zip_paths):
            count, _ = extract_zip(zip_path, destination, allow_existing=index > 0)
            extracted += count
    else:
        if any(zipfile.is_zipfile(path) for path in downloaded):
            raise RuntimeError("iCloud mezclo ZIP y archivos directos; se conservan en Descargas")
        unique_downloads = []
        seen_hashes = set()
        for path in downloaded:
            digest = sha256_file(path)
            if digest in seen_hashes:
                log(f"Descarga duplicada por contenido, se omite: {path.name}")
                continue
            seen_hashes.add(digest)
            unique_downloads.append(path)
        if len(unique_downloads) < expected:
            raise RuntimeError(
                f"iCloud genero {len(unique_downloads)} archivos unicos para {expected} elementos; "
                "se conservan en Descargas y no se copiaran al destino"
            )
        destination.mkdir(parents=True, exist_ok=True)
        for path in unique_downloads:
            target = destination / path.name
            if target.exists():
                raise RuntimeError(f"El archivo ya existe en el destino: {target}")
            shutil.move(str(path), str(target))
        extracted = len(downloaded)
        log(f"Archivos directos colocados en destino: {extracted}")
    files = [item for item in destination.rglob("*") if item.is_file()]
    log(f"Verificacion local: {len(files)} archivos, {sum(item.stat().st_size for item in files) / 1048576:.1f} MB")
    if len(files) != extracted:
        log("Aviso: el numero de archivos extraidos no coincide con el numero de archivos en disco.")
    if CONFIG.get("verify_megasync"):
        log("Esperando a que MEGAsync sincronice...")
        time.sleep(args.sync_wait)
        # Los albumes grandes tardan en subir: reintentar hasta agotar el tiempo maximo.
        deadline = time.time() + args.verify_timeout
        verified = verify_mega_album(storage_album, destination)
        while not verified and time.time() < deadline:
            log("MEGAsync aun sincroniza; se reintenta la verificacion en 60 s...")
            time.sleep(60)
            verified = verify_mega_album(storage_album, destination)
        if not verified:
            log("La verificacion remota no cuadra. Se conserva el ZIP.")
            return 3
        log("Verificacion remota correcta.")
    for zip_path in locals().get("zip_paths", []):
        zip_path.unlink()
        log(f"ZIP verificado y borrado: {zip_path}")
    for staging in {path.parent for path in downloaded}:
        try:
            staging.rmdir()
            log(f"Carpeta temporal vacia eliminada: {staging}")
        except OSError:
            if staging.exists():
                log(f"Carpeta temporal conservada porque contiene archivos: {staging}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Descarga albumes de iCloud Fotos a una carpeta local")
    parser.add_argument("--setup", action="store_true", help="Configura cuenta de iCloud y carpeta de destino")
    parser.add_argument("--login", action="store_true", help="Abre una ventana para iniciar sesion en iCloud")
    parser.add_argument("--inventory", action="store_true", help="Lista todos los albumes de iCloud")
    parser.add_argument("--pending", action="store_true", help="Calcula los albumes que faltan")
    parser.add_argument("--choose-album", action="store_true", help="Elige un album pendiente por numero o nombre")
    parser.add_argument("--all", action="store_true", help="Migra todos los pendientes; los fallos se anotan y se sigue")
    parser.add_argument("--album", help="Migra un album concreto")
    parser.add_argument("--destination-root", help="Sustituye la carpeta de destino de config.json solo en esta ejecucion")
    parser.add_argument("--sync-wait", type=int, default=120, help="Segundos de espera para MEGAsync")
    parser.add_argument("--verify-timeout", type=int, default=1800, help="Segundos extra reintentando la verificacion si MEGAsync no ha terminado")
    parser.add_argument("--timeout", type=int, default=900, help="Segundos maximos para el login")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_config()
    if args.setup:
        return command_setup(args)
    if args.destination_root:
        CONFIG["destination"] = args.destination_root
    if not args.login:
        require_config()
    if args.login:
        return command_login(args)
    if args.inventory:
        return command_inventory(args)
    if args.pending:
        return command_pending(args)
    if args.all:
        return command_all(args)
    if args.choose_album:
        return command_choose_album(args)
    if args.album:
        return command_album(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    session_log = start_session_log()
    exit_code = 0
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr, flush=True)
        exit_code = 130
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        elif exc.code is None:
            exit_code = 0
        else:
            print(exc.code, file=sys.stderr, flush=True)
            exit_code = 1
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        print(
            f"===== Fin {datetime.datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"(codigo {exit_code}) =====",
            flush=True,
        )
        if session_log:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            session_log.close()
    raise SystemExit(exit_code)
