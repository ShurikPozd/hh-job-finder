"""Облачный бэкап SQLite в GitHub через Git Data API.

Free-план Render использует эфемерный диск — при редеплое/переезде БД стирается.
Держим дамп JSON в GitHub-репо: периодический push + восстановление при пустой БД.

Механика (как в tg-saver-bot-cloud): blob -> tree (base_tree) -> commit -> ref.
Git Data API работает для файлов любого размера в отличие от Contents API.
"""
import asyncio
import base64
import json
import logging
import time

import aiohttp

import config

log = logging.getLogger("cloud_backup")


def _backup_name() -> str:
    return "hh_job_finder.json"


def _remote_path() -> str:
    name = _backup_name()
    return f"{config.GITHUB_PATH}/{name}" if config.GITHUB_PATH else name


def _headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    }


async def _git_ref(session) -> str | None:
    url = (f"https://api.github.com/repos/{config.GITHUB_REPO}/"
           f"git/refs/heads/{config.GITHUB_BRANCH}")
    async with session.get(url, headers=_headers(),
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 200:
            return (await resp.json())["object"]["sha"]
        if resp.status == 409:
            log.info("GitHub: репо пустое — создаем init-коммит")
            return await _init_repo(session)
        log.warning("GitHub ref %s: HTTP %s", config.GITHUB_BRANCH, resp.status)
        return None


async def _init_repo(session) -> str | None:
    """Contents API: создаём README.md чтобы сделать первый коммит."""
    import base64 as _b64
    import time as _time
    body = {
        "message": "init: hh-job-finder backups",
        "content": _b64.b64encode(b"# hh-job-finder-backups\n").decode(),
        "branch": config.GITHUB_BRANCH,
    }
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/contents/README.md"
    async with session.put(url, headers=_headers(), json=body,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status in (200, 201):
            commit_sha = (await resp.json())["commit"]["sha"]
            log.info("GitHub init commit: %s", commit_sha[:8])
            return commit_sha
        log.warning("GitHub init не удался: HTTP %s: %s", resp.status,
                    (await resp.text())[:300])
        return None


async def _push_commit(session, path: str, raw: bytes) -> bool:
    """Один цикл blob -> tree -> commit -> ref с обновлённым head."""
    # пустой репозиторий блокирует даже POST /git/blobs — сначала init
    head_sha = await _git_ref(session)
    if not head_sha:
        return False

    blob = {"content": base64.b64encode(raw).decode(), "encoding": "base64"}
    async with session.post(
        f"https://api.github.com/repos/{config.GITHUB_REPO}/git/blobs",
        headers=_headers(), json=blob, timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        if resp.status not in (200, 201):
            log.warning("GitHub blob %s: HTTP %s: %s", path, resp.status,
                        (await resp.text())[:300])
            return False
        blob_sha = (await resp.json())["sha"]

    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/git/commits/{head_sha}"
    async with session.get(url, headers=_headers(),
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            log.warning("GitHub commit %s: HTTP %s", head_sha, resp.status)
            return False
        base_tree = (await resp.json())["tree"]["sha"]

    tree_payload = {
        "base_tree": base_tree,
        "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}],
    }
    async with session.post(
        f"https://api.github.com/repos/{config.GITHUB_REPO}/git/trees",
        headers=_headers(), json=tree_payload, timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status not in (200, 201):
            log.warning("GitHub tree %s: HTTP %s: %s", path, resp.status,
                        (await resp.text())[:300])
            return False
        new_tree = (await resp.json())["sha"]

    commit_payload = {
        "message": f"backup: hh-job-finder ({time.strftime('%Y-%m-%d %H:%M')} UTC)",
        "tree": new_tree,
        "parents": [head_sha],
    }
    async with session.post(
        f"https://api.github.com/repos/{config.GITHUB_REPO}/git/commits",
        headers=_headers(), json=commit_payload, timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status not in (200, 201):
            log.warning("GitHub commit %s: HTTP %s: %s", path, resp.status,
                        (await resp.text())[:300])
            return False
        commit_sha = (await resp.json())["sha"]

    url = (f"https://api.github.com/repos/{config.GITHUB_REPO}/"
           f"git/refs/heads/{config.GITHUB_BRANCH}")
    async with session.patch(
        url, headers=_headers(), json={"sha": commit_sha, "force": False},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            log.warning("GitHub ref update %s: HTTP %s: %s", config.GITHUB_BRANCH,
                        resp.status, (await resp.text())[:200])
            return False
    return True


async def push_backup(db) -> bool:
    """Экспортирует БД в JSON и пушит в GitHub."""
    if not config.GITHUB_TOKEN:
        log.info("GITHUB_TOKEN не задан, бэкап пропущен")
        return False
    try:
        data = await db.export_json()
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception as e:
        log.warning("Экспорт БД для бэкапа не удался: %s", e)
        return False
    if not data.get("tables", {}).get("users"):
        log.info("БД пуста — бэкапить нечего")
        return False
    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(1, 3):
                if await _push_commit(session, _remote_path(), raw):
                    log.info("Бэкап запушен (%d байт)", len(raw))
                    return True
                if attempt == 2:
                    break
                await asyncio.sleep(2)
        return False
    except Exception as e:
        log.warning("GitHub push не удался: %s", e)
        return False


async def _fetch_raw(session, path: str) -> bytes | None:
    url = f"https://api.github.com/repos/{config.GITHUB_REPO}/contents/{path}"
    async with session.get(url, headers=_headers(),
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            log.warning("GitHub READ %s: HTTP %s", path, resp.status)
            return None
        payload = await resp.json()
        content = payload.get("content")
        if not content:
            return None
        return base64.b64decode(content)


async def restore_from_github(db) -> dict:
    """Скачивает последний дамп и импортирует (только в пустые таблицы).

    Возвращает {'restored': N, 'users': M}.
    """
    if not config.GITHUB_TOKEN:
        return {"restored": 0, "users": 0}
    try:
        async with aiohttp.ClientSession() as session:
            raw = await _fetch_raw(session, _remote_path())
    except Exception as e:
        log.warning("GitHub чтение бэкапа не удалось: %s", e)
        return {"restored": 0, "users": 0}
    if not raw:
        return {"restored": 0, "users": 0}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("Бэкап не читается как JSON: %s", e)
        return {"restored": 0, "users": 0}
    try:
        n = await db.import_json(data)
    except Exception as e:
        log.warning("Импорт бэкапа не удался: %s", e)
        return {"restored": 0, "users": 0}
    users = len(data.get("tables", {}).get("users", []))
    log.info("Восстановлено из GitHub: %s записей, %s пользователей", n, users)
    return {"restored": n, "users": users}


async def backup_loop(db):
    """Периодический бэкап раз в BACKUP_INTERVAL_HOURS."""
    while True:
        await asyncio.sleep(config.BACKUP_INTERVAL_HOURS * 3600)
        try:
            await push_backup(db)
        except Exception as e:
            log.warning("backup_loop: %s", e)