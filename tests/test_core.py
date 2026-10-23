"""Creations Browser core: the catalogue client, free check, BTAR reader, zip conversion, .meta, fetch, lookups and local
status. Every network call goes through a fake transport; nothing here reaches the real network."""

from __future__ import annotations

import email.message
import hashlib
import http.client
import io
import json
import re
import ssl
import struct
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
import zipfile
from pathlib import Path

import pytest

from creations_browser import core, texts

from fakes import GOOD_ENTRIES, HOST, KEY, PLUGIN, UUID, Net, Response, api_item, btar, secrets_absent  # noqa: F401

REAL_OPEN = core._open  # captured before any fixture replaces it
REAL_OPENER_OPEN = urllib.request.OpenerDirector.open


# ---------------------------------------------------------------- the package

def test_the_package_imports_without_mobase_or_qt():
    import creations_browser
    assert creations_browser.__version__ == core.VERSION == "1.2.0"
    assert core.USER_AGENT == ("CreationsBrowser/1.2.0 (unofficial Mod Organizer 2 plugin; "
                               "+https://github.com/sayngwyn/creations-browser)")
    assert callable(creations_browser.createPlugin)
    # a clean interpreter (other tests in this run load Qt and the plugin module): the package alone loads neither
    src = str(Path(__file__).resolve().parent.parent / "src")
    code = ("import sys; sys.path.insert(0, %r); import creations_browser, creations_browser.core; "
            "bad = [m for m in ('creations_browser.plugin', 'creations_browser.window', 'mobase', 'PyQt6', 'PyQt5', 'PySide6') "
            "if m in sys.modules]; sys.exit(1 if bad else 0)") % src
    assert subprocess.run([sys.executable, "-I", "-c", code], capture_output=True).returncode == 0


def test_the_user_agent_goes_out_on_catalogue_and_download_requests(net, tmp_path):
    net.add(api_item())
    core.detail("fallout4", 743724)
    core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    agents = {h["user-agent"] for _, h in net.calls}
    assert agents == {core.USER_AGENT}


# ---------------------------------------------------------------- catalogue client

def test_the_seam_is_the_only_door_to_the_network(monkeypatch):
    monkeypatch.setattr(core, "_open", REAL_OPEN)
    with pytest.raises(AssertionError, match="real network"):  # the real seam reaches urllib, which the guard blocks
        core._open(urllib.request.Request("https://example.invalid/"), 1)
    with pytest.raises(AssertionError, match="real network"):
        core.detail("fallout4", 1)  # even the key fetch goes through the seam


def test_search_detail_and_the_key_headers(net):
    net.add(api_item())
    found = core.search("fallout4", text="fine mod")
    assert found[0]["content_uuid"] == UUID
    assert core.detail("fallout4", 743724)["title"] == "Fine Mod: Revamped"
    core.detail("skyrimspecialedition", 743724)
    products = [h["x-bnet-product"] for u, h in net.calls if "api.bethesda.net" in u]
    assert products == ["fallout4", "fallout4", "skyrim"]
    assert sum(u == core.CORE_URL for u, _ in net.calls) == 1  # the key is fetched once per process
    with pytest.raises(core.CreationsError) as exc:
        core.detail("fallout4", 5)
    assert exc.value.status == "NOT_FOUND"


def test_resolve_numeric_uuid_and_link(net):
    net.add(api_item())
    assert core.resolve("fallout4", 743724) == (743724, None)
    assert core.resolve("fallout4", "743724") == (743724, None)
    assert core.resolve("fallout4", UUID.upper()) == (743724, UUID)
    link = f"https://creations.bethesda.net/en/fallout4/details/{UUID}/Fine_Mod"
    assert core.resolve("fallout4", link) == (743724, UUID)
    assert core.resolve("fallout4", link.replace("https://", "")) == (743724, UUID)
    for bad, status in [("https://example.com/details/" + UUID, "INVALID_ARGUMENT"),
                        ("https://creations.bethesda.net/en/fallout4/details/nothing", "INVALID_ARGUMENT"),
                        (link.replace("fallout4", "skyrim"), "INVALID_ARGUMENT"), ("not an id", "INVALID_ARGUMENT"),
                        (-3, "INVALID_ARGUMENT"), (True, "INVALID_ARGUMENT"),
                        ("99999999-2222-3333-4444-555555555555", "NOT_FOUND")]:
        with pytest.raises(core.CreationsError) as exc:
            core.resolve("fallout4", bad)
        assert exc.value.status == status, bad
    with pytest.raises(core.CreationsError, match="game must be"):
        core.resolve("oblivion", 5)


def test_html_escaped_titles_are_shown_as_text(net):
    net.add(api_item(title="Show Prompts &amp; Fixes"))
    assert core.detail("fallout4", 743724)["title"] == "Show Prompts & Fixes"
    assert core.search("fallout4", text="show")[0]["title"] == "Show Prompts & Fixes"
    assert core.zip_name("Show Prompts & Fixes", 7, "1.0") == "Show Prompts & Fixes (Creation 7) v1.0.zip"


def test_free_and_paid(net):
    assert core.is_free(api_item())
    assert not core.is_free(api_item(price="500"))
    assert not core.is_free(api_item(catalog={"sku": "x"}))
    for kw in ({"price": "500"}, {"catalog": {"sku": "x"}}):
        net.add(api_item(**kw))
        with pytest.raises(core.CreationsError) as exc:
            core.plan("fallout4", 743724)
        assert exc.value.status == "NOT_FREE"
    assert not net.downloads()


def test_the_pc_file_is_the_latest_windows_ckm_on_the_right_host():
    item = api_item(ctime=100)
    newer = api_item(version="1.05", ctime=200)["download"][0]["published"][0]
    item["download"][0]["published"].append(newer)
    assert core.pc_file(item)["version"] == "1.05"
    assert core.pc_file(item, "1.04")["version"] == "1.04"
    with pytest.raises(core.CreationsError) as exc:
        core.pc_file(item, "9.9")
    assert exc.value.status == "NOT_FOUND"
    # no Windows group, or a Windows group without a .ckm
    other = api_item()
    other["download"][0]["hardware_platform"] = "XBOXONE"
    with pytest.raises(core.CreationsError) as exc:
        core.pc_file(other)
    assert exc.value.status == "NO_PC_FILE" and "Windows" in str(exc.value)
    noext = api_item()
    for meta in noext["download"][0]["published"][0]["client"].values():
        meta["download_url"] = HOST + "/files/x.zip"
    with pytest.raises(core.CreationsError) as exc:
        core.pc_file(noext)
    assert exc.value.status == "NO_PC_FILE"
    bad = api_item()
    for meta in bad["download"][0]["published"][0]["client"].values():
        meta["download_url"] = "https://evil.example.com/x.ckm?sig=1"
    with pytest.raises(core.CreationsError) as exc:
        core.pc_file(bad)
    assert exc.value.status == "UNEXPECTED_HOST"
    plain = api_item()
    for meta in plain["download"][0]["published"][0]["client"].values():
        meta["download_url"] = meta["download_url"].replace("https://", "http://")
    with pytest.raises(core.CreationsError) as exc:
        core.pc_file(plain)
    assert exc.value.status == "UNEXPECTED_HOST"


# ---------------------------------------------------------------- download checks

def test_download_checks_size_and_md5(net, tmp_path):
    data = btar(GOOD_ENTRIES)
    net.files["/f.ckm"] = data
    url = HOST + "/f.ckm?sig=1"
    md5 = hashlib.md5(data).hexdigest()
    assert core.download_ckm(url, tmp_path / "a", len(data), md5) == md5
    with pytest.raises(core.CreationsError, match="bytes") as exc:
        core.download_ckm(url, tmp_path / "b", len(data) + 1, md5)
    assert exc.value.status == "DOWNLOAD_MISMATCH"
    with pytest.raises(core.CreationsError, match="md5"):
        core.download_ckm(url, tmp_path / "c", len(data), "0" * 32)
    with pytest.raises(core.CreationsError, match="cannot be verified"):
        core.download_ckm(url, tmp_path / "d", None, "")
    core.download_ckm(url, tmp_path / "e", len(data), "abc-2")  # a multipart etag is not an md5: the size is the check
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm("https://evil.example.com/f.ckm", tmp_path / "f", None, md5)
    assert exc.value.status == "UNEXPECTED_HOST"


def test_download_without_a_size_but_with_an_md5_is_accepted(net, tmp_path):
    data = btar(GOOD_ENTRIES)
    net.files["/f.ckm"] = data
    md5 = hashlib.md5(data).hexdigest()
    assert core.download_ckm(HOST + "/f.ckm", tmp_path / "a", None, md5.upper()) == md5


# ---------------------------------------------------------------- progress and cancel

def test_progress_is_reported_per_chunk(monkeypatch, net, tmp_path):
    data = btar(GOOD_ENTRIES)
    net.files["/f.ckm"] = data
    monkeypatch.setattr(core, "CHUNK", 16)
    seen = []
    core.download_ckm(HOST + "/f.ckm", tmp_path / "a", len(data), hashlib.md5(data).hexdigest(), progress=lambda d, t: seen.append((d, t)))
    assert len(seen) == -(-len(data) // 16) and len(seen) > 1
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and seen[-1] == (len(data), len(data))
    assert {t for _, t in seen} == {len(data)}
    seen.clear()  # no listed size: the total is None
    core.download_ckm(HOST + "/f.ckm", tmp_path / "b", None, hashlib.md5(data).hexdigest(), progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (len(data), None) and {t for _, t in seen} == {None}


def test_fetch_passes_progress_through(monkeypatch, net, tmp_path):
    net.add(api_item())
    monkeypatch.setattr(core, "CHUNK", 16)
    seen = []
    core.fetch(tmp_path / "dl", core.plan("fallout4", 743724), progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1] == (len(btar(GOOD_ENTRIES)), len(btar(GOOD_ENTRIES)))


def test_cancel_mid_download_stops_and_leaves_nothing(monkeypatch, net, tmp_path):
    net.add(api_item())
    monkeypatch.setattr(core, "CHUNK", 16)
    chunks = []
    p = core.plan("fallout4", 743724)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p, progress=lambda d, t: chunks.append(d), cancel=lambda: len(chunks) >= 2)
    assert exc.value.status == "CANCELLED" and len(chunks) == 2
    assert list((tmp_path / "dl").iterdir()) == []  # no .part, no zip, no .meta


def test_cancel_before_the_request_makes_no_download_call(net, tmp_path):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p, cancel=lambda: True)
    assert exc.value.status == "CANCELLED" and not net.downloads() and list((tmp_path / "dl").iterdir()) == []


def test_cancel_removes_a_partial_file_of_a_direct_download(monkeypatch, net, tmp_path):
    net.files["/f.ckm"] = b"x" * 100
    monkeypatch.setattr(core, "CHUNK", 10)
    calls = []
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "a.part", 100, "", cancel=lambda: calls.append(1) or len(calls) > 3)
    assert exc.value.status == "CANCELLED" and not (tmp_path / "a.part").exists()


def test_a_cancel_that_never_fires_changes_nothing(net, tmp_path):
    net.add(api_item())
    got, _, reused = core.fetch(tmp_path / "dl", core.plan("fallout4", 743724), cancel=lambda: False)
    assert zipfile.is_zipfile(got) and not reused


# ---------------------------------------------------------------- the BTAR reader

def convert(tmp_path, data):
    ckm = tmp_path / "x.ckm"
    ckm.write_bytes(data)
    return core.ckm_to_zip(ckm, tmp_path / "x.zip")


def test_a_good_ckm_becomes_a_stored_deterministic_zip(tmp_path):
    out = convert(tmp_path, btar(GOOD_ENTRIES))
    assert out["files"] == ["Data/Fine.esp", "Data/Fine.ini", "Data/Fine - Main.ba2"]
    first = (tmp_path / "x.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "x.zip") as zf:
        assert zf.testzip() is None
        assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
        assert zf.read("Data/Fine.esp") == PLUGIN and zf.read("Data/Fine.ini") == b""
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())
    (tmp_path / "x.zip").unlink()
    convert(tmp_path, btar(GOOD_ENTRIES))
    assert (tmp_path / "x.zip").read_bytes() == first


@pytest.mark.parametrize("data,status,words", [
    (btar(GOOD_ENTRIES, magic=b"BTAX"), "INVALID_CKM", "magic"),
    (b"BT", "INVALID_CKM", "magic"),
    (btar(GOOD_ENTRIES, version=2), "UNSUPPORTED_CKM", "version 2"),
    (btar([]), "INVALID_CKM", "no files"),
    (btar(GOOD_ENTRIES, count=4), "INVALID_CKM", "truncated"),
    (btar(GOOD_ENTRIES)[:-3], "INVALID_CKM", "truncated"),
    (btar(GOOD_ENTRIES)[:11], "INVALID_CKM", "truncated"),
    (btar(GOOD_ENTRIES, extra=b"\x00"), "INVALID_CKM", "trailing"),
    (btar(GOOD_ENTRIES, count=2), "INVALID_CKM", "trailing"),
    (btar([("", b"x")]), "UNSAFE_CKM", "empty path"),
    (btar([("c:\\evil.esp", b"x")]), "UNSAFE_CKM", "unsafe"),
    (btar([("\\evil.esp", b"x")]), "UNSAFE_CKM", "unsafe"),
    (btar([("data\\..\\..\\evil.esp", b"x")]), "UNSAFE_CKM", "unsafe"),
    (btar([("data\\\\x.esp", b"x")]), "UNSAFE_CKM", "unsafe"),
    (btar([("data\\x:stream.esp", b"x")]), "UNSAFE_CKM", "unsafe"),
    (btar([("data\\Run.dll", b"x")]), "UNSAFE_CKM", "isn't a type this plugin accepts"),
    (btar([("data\\Run.BAT", b"x")]), "UNSAFE_CKM", "isn't a type this plugin accepts"),
    (btar([("data\\Run.ps1", b"x")]), "UNSAFE_CKM", "isn't a type this plugin accepts"),
    (btar([("data\\mystery.bin", b"x")]), "UNSAFE_CKM", "isn't a type this plugin accepts"),
    (btar([("data\\noextension", b"x")]), "UNSAFE_CKM", "isn't a type this plugin accepts"),
    (btar([("Root\\skse64_loader.esp", b"x")]), "UNSAFE_CKM", "isn't inside the Data folder"),
    (btar([("Fine.esp", b"x")]), "UNSAFE_CKM", "isn't inside the Data folder"),
    (btar([("data", b"x")]), "UNSAFE_CKM", "isn't inside the Data folder"),
    (btar([("Fallout4 - Main.ba2", b"x"), ("data\\a.esp", b"y")]), "UNSAFE_CKM", "isn't inside the Data folder"),
    (btar([("data\\CON.esp", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\nul.txt", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\Aux", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\textures\\COM1.dds", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\lpt9.esp", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\COM¹.esp", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\lpt².esp", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\com0.dds", b"x")]), "UNSAFE_CKM", "device name"),
    (btar([("data\\ＣＯＮ.esp", b"x")]), "UNSAFE_CKM", "device name"),  # full-width letters fold to CON under NFKC
    (btar([("data\\a.esp", b"x"), ("DATA\\A.ESP", b"y")]), "UNSAFE_CKM", "twice"),
    (btar([("data\\a.esp", b"x"), ("data\\a.esp\\b.esp", b"y")]), "UNSAFE_CKM", "also a file"),
])
def test_a_bad_ckm_is_refused_and_leaves_no_zip_content(tmp_path, data, status, words):
    with pytest.raises(core.CreationsError, match=words) as exc:
        convert(tmp_path, data)
    assert exc.value.status == status


def test_a_non_utf8_path_is_refused(tmp_path):
    raw = b"BTAR" + struct.pack("<HH", 1, 1) + struct.pack("<H", 2) + b"\xff\xfe" + struct.pack("<Q", 1) + b"x"
    with pytest.raises(core.CreationsError, match="UTF-8"):
        convert(tmp_path, raw)


def test_loose_game_assets_are_allowed_and_names_that_only_look_reserved_are_fine(tmp_path):
    out = convert(tmp_path, btar([("data\\Fine.esp", PLUGIN), ("data\\Textures\\a.DDS", b"d"), ("data\\Meshes\\b.nif", b"n"),
                                  ("data\\Scripts\\C.pex", b"p"), ("data\\Strings\\F_en.STRINGS", b"s"),
                                  ("data\\console.esp", b"c"), ("data\\comet.txt", b"t"), ("data\\Fine - Main.ba2", b"b")]))
    assert out["files"][1:6] == ["Data/Textures/a.DDS", "Data/Meshes/b.nif", "Data/Scripts/C.pex", "Data/Strings/F_en.STRINGS", "Data/console.esp"]


# ---------------------------------------------------------------- zip and meta in downloads

def test_fetch_writes_a_named_zip_with_provenance_and_reuses_it(net, tmp_path):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    assert "SIGNED-SECRET" not in json.dumps(p.public()) and "SIGNED-SECRET" not in repr(p)
    got, info, reused = core.fetch(tmp_path / "dl", p)
    assert not reused and got.name == "Fine Mod_ Revamped (Creation 743724) v1.04.zip" and zipfile.is_zipfile(got)
    meta = core.read_meta(got)
    assert meta["repository"] == "Bethesda.net" and meta["creationscontentid"] == "743724" and meta["creationsuuid"] == UUID
    assert meta["creationsversion"] == "1.04" and meta["creationsckmmd5"] == hashlib.md5(btar(GOOD_ENTRIES)).hexdigest()
    assert meta["url"] == f"https://creations.bethesda.net/en/fallout4/details/{UUID}" and meta["modid"] == "-1" and meta["modname"] == "Fine Mod: Revamped"
    assert meta["gamename"] == "Fallout4" and meta["installed"] == "false"
    secrets_absent(info)
    assert not list((tmp_path / "dl").glob("*.part")) and len(net.downloads()) == 1
    again, _, reused = core.fetch(tmp_path / "dl", p)
    assert reused and again == got and len(net.downloads()) == 1
    # the zip is deterministic: with its .meta lost the same bytes are accepted, other bytes are never overwritten
    Path(str(got) + ".meta").unlink()
    same, _, reused = core.fetch(tmp_path / "dl", p)
    assert same == got and not reused and Path(str(got) + ".meta").is_file()
    Path(str(got) + ".meta").unlink()
    got.write_bytes(b"other")
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p)
    assert exc.value.status == "TARGET_EXISTS" and got.read_bytes() == b"other"


def test_the_meta_is_written_in_mo2s_form_with_mo2s_game_names(net, tmp_path):
    net.add(api_item())
    p = core.plan("skyrimspecialedition", 743724)
    got, _, _ = core.fetch(tmp_path / "dl", p)
    raw = Path(str(got) + ".meta").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf[General]\r\ngameName=SkyrimSE\r\n") and raw.endswith(b"\r\n")
    assert core.read_meta(got)["url"] == f"https://creations.bethesda.net/en/skyrim/details/{UUID}"
    # MO2's own spelling and the long lower-case form are both understood when comparing
    assert core.game_alias("SkyrimSE") == core.game_alias("skyrimspecialedition") == "skyrimspecialedition"
    assert core.game_alias("Fallout4") == core.game_alias("fallout4") == "fallout4"
    assert core.find_downloaded(tmp_path / "dl", "skyrimspecialedition", 743724, "1.04") == got
    assert core.find_downloaded(tmp_path / "dl", "fallout4", 743724, "1.04") is None  # another game's download
    meta = Path(str(got) + ".meta")
    meta.write_bytes(meta.read_bytes().replace(b"gameName=SkyrimSE", b"gameName=skyrimspecialedition"))
    assert core.find_downloaded(tmp_path / "dl", "skyrimspecialedition", 743724, "1.04") == got


def test_read_meta_reads_general_only_and_tolerates_absence(tmp_path):
    z = tmp_path / "a.zip"
    assert core.read_meta(z) == {}
    Path(str(z) + ".meta").write_text("\ufeff[General]\r\nGameName = SkyrimSE\r\nfoo=a=b\r\n[Other]\r\nfoo=no\r\n", encoding="utf-8")
    assert core.read_meta(z) == {"gamename": "SkyrimSE", "foo": "a=b"}


def test_a_failed_download_leaves_nothing_behind(net, tmp_path):
    bad = api_item()
    net.add(bad)
    for pub in bad["download"][0]["published"]:
        for meta in pub["client"].values():
            meta["size"] += 1
    p = core.plan("fallout4", 743724)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p)
    assert exc.value.status == "DOWNLOAD_MISMATCH" and list((tmp_path / "dl").iterdir()) == []
    net.add(api_item(ckm=btar([("data\\x.dll", b"x")]), cid=743724), ckm=btar([("data\\x.dll", b"x")]))
    p = core.plan("fallout4", 743724)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p)
    assert exc.value.status == "UNSAFE_CKM" and list((tmp_path / "dl").iterdir()) == []


def test_a_creation_with_a_bad_container_or_no_pc_file_is_refused_without_residue(net, tmp_path):
    item = api_item()
    item["download"][0]["hardware_platform"] = "XBOXONE"
    net.add(item)
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 743724)
    assert exc.value.status == "NO_PC_FILE"
    trailing = btar(GOOD_ENTRIES, extra=b"zz")
    net.add(api_item(ckm=trailing), ckm=trailing)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    assert exc.value.status == "INVALID_CKM" and list((tmp_path / "dl").iterdir()) == []


def test_a_paid_creation_is_never_fetched_and_a_wrong_game_is_refused(net):
    net.add(api_item(price="300"))
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 743724)
    assert exc.value.status == "NOT_FREE" and not net.downloads()
    with pytest.raises(core.CreationsError) as exc:
        core.plan("oblivion", 743724)
    assert exc.value.status == "INVALID_ARGUMENT"
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 5)
    assert exc.value.status == "NOT_FOUND"


def test_plan_public_and_info_never_carry_secrets(net, tmp_path):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    _, info, _ = core.fetch(tmp_path / "dl", p)
    secrets_absent(p.public(), info)
    assert p.public()["bytes"] == len(btar(GOOD_ENTRIES)) and p.public()["free"] is True and "url" not in p.public()


def test_a_network_failure_mid_download_through_fetch_is_coded_and_clean(net, tmp_path, monkeypatch):
    net.add(api_item())
    real = net.__call__

    def flaky(request, timeout):
        if "ugcmods.bethesda.net" in request.full_url:
            raise http.client.IncompleteRead(b"")
        return real(request, timeout)
    monkeypatch.setattr(core, "_open", flaky)
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    assert exc.value.status == "UNREACHABLE" and list((tmp_path / "dl").iterdir()) == []


# ---------------------------------------------------------------- lookups for the UI

def test_lookup_search_and_detail_never_leak_keys_or_signed_links(net):
    net.add(api_item(required_mods=[1001, 1002], required_dlc=["DLCCoast"]))
    net.add(api_item(cid=1001, uuid="aaaaaaaa-2222-3333-4444-555555555555", title="Base Library"))
    net.add(api_item(cid=1002, uuid="bbbbbbbb-2222-3333-4444-555555555555", title="Paid Lib", price="100"))
    found = core.lookup_search("fallout4", "fine mod")
    assert isinstance(found, list) and found[0]["title"] == "Fine Mod: Revamped" and found[0]["free"] is True
    assert found[0]["page"] == f"https://creations.bethesda.net/en/fallout4/details/{UUID}"
    assert found[0]["get_in_game_reason"] is None
    one = core.lookup_detail("fallout4", 743724)
    assert one["pc_file"] == {"version": "1.04", "bytes": len(btar(GOOD_ENTRIES)), "file": "743724.ckm", "ctime": 100}
    assert [(m["content_id"], m["title"], m["free"]) for m in one["required_mods"]] == [(1001, "Base Library", True), (1002, "Paid Lib", False)]
    assert one["required_dlc"] == ["DLCCoast"] and one["free"] is True and one["uuid"] == UUID and one["game"] == "fallout4"
    by_uuid = core.lookup_detail("fallout4", UUID)
    assert by_uuid["content_id"] == 743724
    secrets_absent(found, one, by_uuid)
    text = json.dumps([found, one, by_uuid])
    assert "?" not in text  # no query string (signed link) anywhere
    paid = core.lookup_detail("fallout4", 1002)
    assert paid["free"] is False and paid["author_price"] == "100"
    assert not net.downloads()  # a lookup never downloads anything


def test_search_results_carry_the_latest_version_and_size_when_the_listing_has_them(net):
    item = api_item(ctime=100)
    item["download"][0]["published"].append(api_item(version="1.05", ctime=200)["download"][0]["published"][0])
    net.add(item)
    net.items[743724] = item
    full = core._summary("fallout4", item)
    assert full["version"] == "1.05" and full["bytes"] == len(btar(GOOD_ENTRIES))
    # the fake search drops the download tree, as a listing may: version and bytes are then None
    listed = core.lookup_search("fallout4", "fine")[0]
    assert listed["version"] is None and listed["bytes"] is None
    detail = core.lookup_detail("fallout4", 743724)
    assert detail["version"] == "1.05" and detail["versions"] == ["1.05", "1.04"]


def test_lookup_reports_why_a_creation_is_not_downloadable(net):
    net.add(api_item(price_usd="5"))
    one = core.lookup_detail("fallout4", 743724)
    assert one["free"] is False and one["free_status"] == "paid" and one["get_in_game_reason"] == "Paid Creation: get it in game"
    unclear = api_item(cid=2, uuid="cccccccc-2222-3333-4444-555555555555", title="Unclear")
    del unclear["catalog_info"]
    net.add(unclear)
    two = core.lookup_detail("fallout4", 2)
    assert two["free"] is False and two["free_status"] == "unknown"
    assert two["get_in_game_reason"] == "Not clearly marked free: get it in game"
    assert core.lookup_search("fallout4", "unclear")[0]["get_in_game_reason"] == "Not clearly marked free: get it in game"


def test_descriptions_are_cleaned_but_keep_their_line_breaks(net):
    net.add(api_item(description="First line.\r\n\r\n\r\n\r\nSecond &amp; third\x00<b>bold</b><br>next\u202e\tend"))
    one = core.lookup_detail("fallout4", 743724)
    assert one["description"] == "First line.\n\nSecond & third bold\nnext end"
    assert core.lookup_search("fallout4", "fine")[0]["description"] == one["description"]
    net.add(api_item(cid=5, uuid="dddddddd-2222-3333-4444-555555555555", title="Other", overview="An overview", description=None))
    assert core.lookup_detail("fallout4", 5)["description"] == "An overview"
    net.add(api_item(cid=6, uuid="eeeeeeee-2222-3333-4444-555555555555", title="Bare"))
    assert core.lookup_detail("fallout4", 6)["description"] == ""


def test_lookup_errors_are_coded(net):
    def status(fn, *a, **k):
        with pytest.raises(core.CreationsError) as exc:
            fn(*a, **k)
        return exc.value.status
    assert status(core.lookup_search, "fallout4", "  ") == "INVALID_ARGUMENT"
    assert status(core.lookup_search, "fallout4", None) == "INVALID_ARGUMENT"
    assert status(core.lookup_search, "oblivion", "x") == "INVALID_ARGUMENT"
    assert status(core.lookup_detail, "oblivion", 1) == "INVALID_ARGUMENT"
    assert status(core.lookup_detail, "fallout4", 99) == "NOT_FOUND"
    assert status(core.lookup_detail, "fallout4", "https://example.com/x") == "INVALID_ARGUMENT"
    no_pc = api_item()
    no_pc["download"] = []
    net.add(no_pc)
    one = core.lookup_detail("fallout4", 743724)
    assert one["pc_file"] is None and "Windows" in one["pc_file_problem"] and one["version"] is None


@pytest.mark.parametrize("limit", [0, -1, 51, "10", 2.5, True, None])
def test_a_bad_limit_is_an_invalid_argument(net, limit):
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_search("fallout4", "x", limit=limit)
    assert exc.value.status == "INVALID_ARGUMENT"


@pytest.mark.parametrize("ref", ["²", "٣", "１２", "12.5", "1 2"])
def test_non_ascii_digits_are_not_a_content_id(net, ref):
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_detail("fallout4", ref)
    assert exc.value.status == "INVALID_ARGUMENT"
    assert core.numeric_id(ref) is None


def test_required_mods_that_are_not_ids_do_not_break_a_lookup(net):
    net.add(api_item(required_mods=["²", None, 1001, {"x": 1}]))
    net.add(api_item(cid=1001, uuid="aaaaaaaa-2222-3333-4444-555555555555", title="Lib"))
    one = core.lookup_detail("fallout4", 743724)
    assert [m["title"] for m in one["required_mods"]] == [None, "Lib"]


def test_the_search_limit_reaches_the_catalogue(net):
    net.add(api_item())
    core.lookup_search("fallout4", "fine", limit=7)
    assert any("size=7" in u for u, _ in net.calls)


# ---------------------------------------------------------------- local status

def fetched(net, tmp_path, game="fallout4", cid=743724, version="1.04", ctime=100, title="Fine Mod"):
    net.add(api_item(cid=cid, version=version, ctime=ctime, title=title))
    return core.fetch(tmp_path / "dl", core.plan(game, cid, version))[0]


def test_local_status_none_downloaded_and_update(net, tmp_path):
    dl = tmp_path / "dl"
    assert core.local_status(dl, "fallout4", 743724, "1.04") == {"state": "none", "archive": None, "version": None, "meta_installed": False}
    zipped = fetched(net, tmp_path)
    got = core.local_status(dl, "fallout4", 743724, "1.04")
    assert got == {"state": "downloaded", "archive": str(zipped), "version": "1.04", "meta_installed": False}
    update = core.local_status(dl, "fallout4", 743724, "1.05")
    assert update == {"state": "update", "archive": str(zipped), "version": "1.04", "meta_installed": False}
    # once the new version is downloaded too, that one is the answer
    new = fetched(net, tmp_path, version="1.05", ctime=200)
    assert core.local_status(dl, "fallout4", 743724, "1.05")["archive"] == str(new)
    assert core.local_status(dl, "fallout4", 743724, "1.05")["state"] == "downloaded"
    assert core.local_status(dl, "fallout4", 743724, "1.04")["archive"] == str(zipped)
    assert core.local_status(dl, "fallout4", 743724, "1.06")["state"] == "update"
    assert core.local_status(dl, "fallout4", 743724, None)["state"] == "downloaded"  # latest unknown: any download counts


def test_local_status_ignores_other_games_creations_and_foreign_downloads(net, tmp_path):
    dl = tmp_path / "dl"
    fetched(net, tmp_path)
    assert core.local_status(dl, "skyrimspecialedition", 743724, "1.04")["state"] == "none"
    assert core.local_status(dl, "skyrim", 743724, "1.04")["state"] == "none"
    assert core.local_status(dl, "fallout4", 999, "1.04")["state"] == "none"
    assert core.local_status(tmp_path / "missing", "fallout4", 743724, "1.04")["state"] == "none"
    # a Nexus download with the same numbers, and a meta whose archive is gone, are not Creations downloads
    (dl / "other.zip").write_bytes(b"x")
    Path(dl / "other.zip.meta").write_text("[General]\r\ngameName=Fallout4\r\nrepository=Nexus\r\ncreationsContentId=555\r\n", encoding="utf-8")
    Path(dl / "gone.zip.meta").write_text("[General]\r\ngameName=Fallout4\r\nrepository=Bethesda.net\r\ncreationsContentId=555\r\n", encoding="utf-8")
    assert core.local_status(dl, "fallout4", 555, "1.0")["state"] == "none"
    # the same id on the other game is told apart by the game name in the meta
    sky = fetched(net, tmp_path, game="skyrimspecialedition", cid=743724, version="2.0", ctime=300, title="Sky Copy")
    assert core.local_status(dl, "skyrimspecialedition", 743724, "2.0") == {
        "state": "downloaded", "archive": str(sky), "version": "2.0", "meta_installed": False}
    assert core.local_status(dl, "fallout4", 743724, "2.0")["state"] == "update"  # only the 1.04 Fallout 4 download counts there


def test_local_status_reads_mo2s_installed_flag(net, tmp_path):
    dl = tmp_path / "dl"
    zipped = fetched(net, tmp_path)
    meta = Path(str(zipped) + ".meta")
    meta.write_bytes(meta.read_bytes().replace(b"installed=false", b"installed=true"))
    assert core.local_status(dl, "fallout4", 743724, "1.04")["meta_installed"] is True
    assert core.local_status(dl, "fallout4", 743724, "1.05") == {
        "state": "update", "archive": str(zipped), "version": "1.04", "meta_installed": True}


def test_local_status_accepts_mo2s_game_names_and_string_ids(net, tmp_path):
    dl = tmp_path / "dl"
    zipped = fetched(net, tmp_path)
    assert core.local_status(dl, "Fallout4", "743724", "1.04")["archive"] == str(zipped)
    assert core.local_status(dl, "Fallout 4", 743724, "1.04")["state"] == "downloaded"


# ---------------------------------------------------------------- redirects and hosts

class RedirectingHandler(urllib.request.BaseHandler):
    """A fake https server that answers every request with a redirect."""
    handler_order = 100

    def https_open(self, req):
        headers = email.message.Message()
        headers["Location"] = "http://evil.example/steal.ckm?sig=SECRET"
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        response.msg = "Found"
        return response


def test_the_real_opener_refuses_every_redirect():
    opener = urllib.request.build_opener(core._NoRedirect, RedirectingHandler)
    request = urllib.request.Request("https://api.bethesda.net/ugcmods/v1/content", headers={"x-bnet-key": KEY})
    with pytest.raises(core.CreationsError, match="refused a redirect") as exc:
        REAL_OPENER_OPEN(opener, request, timeout=5)  # the fake server is in-process; only the guard is bypassed
    assert exc.value.status == "CREATIONS_ERROR" and "SECRET" not in str(exc.value) and KEY not in str(exc.value)


def test_the_seam_uses_the_refusing_opener(monkeypatch):
    built = []

    class Opener:
        def open(self, request, timeout):
            return Response(b"{}")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: (built.append(handlers), Opener())[1])
    REAL_OPEN(urllib.request.Request("https://example.invalid/"), 5)
    assert built == [(core._NoRedirect,)]


def test_a_refused_redirect_is_a_coded_failure_for_the_catalogue_and_the_download(monkeypatch, tmp_path):
    def redirect(request, timeout):
        raise core.CreationsError("CREATIONS_ERROR", "refused a redirect (HTTP 302) to http://evil.example/x")
    monkeypatch.setattr(core, "_open", redirect)
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_search("fallout4", "x")
    assert exc.value.status == "CREATIONS_ERROR"
    with pytest.raises(core.CreationsError, match="redirect"):
        core.download_ckm(HOST + "/f.ckm", tmp_path / "x", 10, "")


def test_the_key_goes_only_to_the_api_host_over_https():
    with pytest.raises(core.CreationsError) as exc:
        core._http_json("https://cdn.bethesda.net/x", {"x-bnet-key": KEY}, "x")
    assert exc.value.status == "UNEXPECTED_HOST" and KEY not in str(exc.value)
    for url in ("http://api.bethesda.net/ugcmods/v1/content", "https://evil.example/ugcmods/v1/content"):
        with pytest.raises(core.CreationsError) as exc:
            core._http_json(url, {"x-bnet-key": KEY}, "x")
        assert exc.value.status == "UNEXPECTED_HOST"


def test_the_key_is_never_sent_to_the_config_host_or_the_download_host(net, tmp_path):
    net.add(api_item())
    core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    for url, headers in net.calls:
        host = url.split("/")[2]
        assert ("x-bnet-key" in headers) == (host == "api.bethesda.net"), url


def test_downloads_are_https_only(net, tmp_path):
    net.files["/f.ckm"] = b"x"
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm("http://ugcmods.bethesda.net/f.ckm", tmp_path / "x", 1, "")
    assert exc.value.status == "UNEXPECTED_HOST" and not net.calls


# ---------------------------------------------------------------- untrusted text

CRAFTED = "Evil&#10;injected=1 more=2\x0bz=3\x85w=4‮RTL"


def test_clean_text_removes_every_line_break_and_control_character():
    out = core.clean_text(CRAFTED)
    assert out == "Evil injected=1 more=2 z=3 w=4RTL"
    for ch in "\n\r\x0b\x0c\x85  \x00\t":
        assert core.clean_text(f"a{ch}b") in ("a b", "ab")
    assert not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in core.clean_text("x\x1f​y z"))


def test_clean_paragraphs_keeps_line_breaks_and_nothing_else_dangerous():
    assert core.clean_paragraphs("a\r\nb\rc\u2028d\u2029e\x85f") == "a\nb\nc\nd\ne\nf"
    assert core.clean_paragraphs("a\n\n\n\n\nb\n\nc") == "a\n\nb\n\nc"
    assert core.clean_paragraphs("  a  \n  b\t\tc  ") == "a\nb c"
    out = core.clean_paragraphs("x\x00\x07y\u202ez\u200bw\x1f!\n")
    assert not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in out if c != "\n")
    assert core.clean_paragraphs("Tom &amp; Jerry &lt;3") == "Tom & Jerry <3"
    assert core.clean_paragraphs("<p>one</p><p>two<br/>three</p><img src='http://evil/x.png'>") == "one\ntwo\nthree"
    assert core.clean_paragraphs("a < b and c > d") == "a < b and c > d"
    assert core.clean_paragraphs(None) == "" and core.clean_paragraphs(5) == "5"
    long = core.clean_paragraphs("word " * 5000)
    assert len(long) <= core.DESCRIPTION_LIMIT and long.endswith("…")
    assert core.clean_paragraphs("x" * 10, limit=5) == "xxxx…"


def test_a_crafted_title_and_version_cannot_inject_keys(net, tmp_path):
    net.add(api_item(title=CRAFTED, version="1.0\ninjected=1\r\n[Evil]"))
    got, info, _ = core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    meta_text = Path(str(got) + ".meta").read_text(encoding="utf-8-sig")
    lines = meta_text.splitlines()
    assert not any(l.startswith(("injected=", "more=", "z=", "w=", "[Evil]")) for l in lines), meta_text
    assert not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in meta_text if c not in "\r\n﻿"), meta_text
    assert [l for l in lines if l.startswith("[")] == ["[General]"]
    assert all(unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp") for c in got.name)
    meta = core.read_meta(got)
    assert meta["name"] == "Evil injected=1 more=2 z=3 w=4RTL" and meta["version"] == "1.0 injected=1 [Evil]"
    assert meta["creationsversion"] == "1.0 injected=1 [Evil]"
    # and the cleaned version is what local_status compares against
    assert core.local_status(tmp_path / "dl", "fallout4", 743724, "1.0\ninjected=1\r\n[Evil]")["state"] == "downloaded"


def test_a_content_uuid_must_be_exactly_a_uuid(net):
    for bad in (UUID + "\nx=1", "x" + UUID, UUID + "0", "not-a-uuid", None, 5):
        net.items.clear()
        net.add(api_item(uuid=bad))
        assert core.detail("fallout4", 743724)["content_uuid"] is None
        with pytest.raises(core.CreationsError) as exc:
            core.plan("fallout4", 743724)
        assert exc.value.status == "CREATIONS_ERROR" and "UUID" in str(exc.value)
    net.items.clear()
    net.add(api_item(uuid=UUID + "0"))
    with pytest.raises(core.CreationsError) as exc:
        core.resolve("fallout4", UUID)
    assert exc.value.status == "NOT_FOUND"


# ---------------------------------------------------------------- free by confirmation

@pytest.mark.parametrize("field,value", [
    ("price_usd", "5.00"), ("purchase_state", "owned"), ("sku_id", "abc"), ("entitlement", True), ("catalogue_entry", {"id": 1}),
    ("author_price", 0.99), ("catalog_info", [{"sku": "x"}]), ("Price", 3),
])
def test_any_paid_looking_field_makes_it_not_free(net, field, value):
    item = {**api_item(), field: value}  # a renamed or extra field next to the usual (empty) ones
    assert core.free_status(item) == "paid" and not core.is_free(item)
    net.add(item)
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 743724)
    assert exc.value.status == "NOT_FREE" and not net.downloads()


def test_free_needs_both_fields_present_and_empty(net):
    for price in (None, "", 0, "0", "0.00", 0.0):
        for catalog in (None, [], {}, ""):
            assert core.free_status({**api_item(), "author_price": price, "catalog_info": catalog}) == "free"
    for drop in ("author_price", "catalog_info"):
        item = api_item()
        del item[drop]
        net.add(item)
        assert core.free_status(item) == "unknown"
        with pytest.raises(core.CreationsError) as exc:
            core.plan("fallout4", 743724)
        assert exc.value.status == "FREE_UNKNOWN"
    assert not net.downloads()


def test_the_download_tree_does_not_count_as_a_paid_field(net):
    item = api_item()
    item["download"][0]["published"][0]["sku_note"] = "x"  # not a catalogue field
    assert core.free_status(item) == "free"


# ---------------------------------------------------------------- caps, deadlines, network errors

class Endless:
    """A response that never ends; counts what was asked for."""
    def __init__(self):
        self.total = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        n = 1 << 20 if n is None or n < 0 else n
        self.total += n
        return b"x" * n


def test_download_stops_reading_once_the_listed_size_is_exceeded(monkeypatch, tmp_path):
    stream = Endless()
    monkeypatch.setattr(core, "_open", lambda request, timeout: stream)
    with pytest.raises(core.CreationsError, match="more than the 100 bytes") as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "x", 100, "")
    assert exc.value.status == "DOWNLOAD_MISMATCH" and stream.total <= 101


def test_download_with_no_size_has_a_hard_cap(monkeypatch, tmp_path):
    stream = Endless()
    monkeypatch.setattr(core, "_open", lambda request, timeout: stream)
    monkeypatch.setattr(core, "MAX_DOWNLOAD", 5000)
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "x", None, "0" * 32)
    assert exc.value.status == "TOO_BIG" and stream.total <= 5001
    monkeypatch.setattr(core, "_open", lambda *a: (_ for _ in ()).throw(AssertionError("a listed size over the cap needs no request")))
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "y", 5001, "")
    assert exc.value.status == "TOO_BIG"


def test_a_slow_download_hits_the_overall_deadline(monkeypatch, net, tmp_path):
    net.files["/f.ckm"] = b"x" * 10
    monkeypatch.setattr(core, "DOWNLOAD_DEADLINE", -1.0)
    with pytest.raises(core.CreationsError, match="took longer") as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "x", 10, "")
    assert exc.value.status == "CREATIONS_ERROR"


def test_catalogue_answers_are_size_capped(monkeypatch, net):
    net.add(api_item())
    monkeypatch.setattr(core, "MAX_JSON", 50)
    with pytest.raises(core.CreationsError, match="larger than") as exc:
        core.detail("fallout4", 743724)
    assert exc.value.status == "CREATIONS_ERROR"


NET_FAILURES = [OSError("boom"), ConnectionResetError("reset"), TimeoutError("slow"), ssl.SSLError("bad certificate"),
                http.client.IncompleteRead(b"ab", 5), http.client.RemoteDisconnected("closed"), urllib.error.URLError("dns")]


@pytest.mark.parametrize("failure", NET_FAILURES, ids=lambda f: type(f).__name__)
def test_network_failures_are_coded_never_internal(monkeypatch, tmp_path, failure):
    def failing(request, timeout):
        raise failure
    monkeypatch.setattr(core, "_open", failing)
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_search("fallout4", "x")
    assert exc.value.status == "UNREACHABLE" and "unreachable" in str(exc.value)
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "x", 5, "")
    assert exc.value.status == "UNREACHABLE"

    class Breaks(Response):
        def read(self, n=-1):
            raise failure
    monkeypatch.setattr(core, "_open", lambda request, timeout: Breaks(b""))
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(HOST + "/f.ckm", tmp_path / "y", 5, "")
    assert exc.value.status == "UNREACHABLE"
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_detail("fallout4", 5)
    assert exc.value.status == "UNREACHABLE"


@pytest.mark.parametrize("code,status", [(429, "RATE_LIMITED"), (500, "CREATIONS_ERROR"), (503, "CREATIONS_ERROR"), (404, "NOT_FOUND")])
def test_http_errors_are_told_apart_from_an_unreachable_service(monkeypatch, tmp_path, code, status):
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(core, "_open", refused)
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_search("fallout4", "x")
    assert exc.value.status == status
    if code != 404:
        with pytest.raises(core.CreationsError) as exc:
            core.download_ckm(HOST + "/f.ckm", tmp_path / "x", 5, "")
        assert exc.value.status == status and not (tmp_path / "x").exists()


# ---------------------------------------------------------------- milestone 2: marketplace, withdrawn, notes, preview

def test_a_marketplace_listing_is_never_free(net):
    item = api_item(marketplace=True)
    assert core.free_status(item) == "paid" and not core.is_free(item)
    assert core.free_status(api_item(marketplace=False)) == "free"
    net.add(item)
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 743724)
    assert exc.value.status == "NOT_FREE" and "marketplace" in str(exc.value) and not net.downloads()
    one = core.lookup_detail("fallout4", 743724)
    assert one["free"] is False and one["downloadable"] is False and one["get_in_game_reason"] == "Paid Creation: get it in game"


@pytest.mark.parametrize("flag", ["deleted", "blocklisted", "restricted"])
def test_withdrawn_creations_are_refused_and_not_downloadable(net, flag):
    net.add(api_item(**{flag: True}))
    with pytest.raises(core.CreationsError) as exc:
        core.plan("fallout4", 743724)
    assert exc.value.status == "UNAVAILABLE" and not net.downloads()
    one = core.lookup_detail("fallout4", 743724)
    assert one["free"] is True and one["downloadable"] is False and one["withdrawn"] is True
    assert one["get_in_game_reason"] == "Withdrawn or restricted by Bethesda: not available here"
    listed = core.lookup_search("fallout4", "fine")[0]
    assert listed["downloadable"] is False and listed["get_in_game_reason"] == one["get_in_game_reason"]


def test_downloadable_is_free_and_not_withdrawn_and_false_flags_do_not_count(net):
    net.add(api_item(deleted=False, blocklisted=False, restricted=False, marketplace=False))
    assert core.lookup_search("fallout4", "fine")[0]["downloadable"] is True
    assert core.lookup_detail("fallout4", 743724)["downloadable"] is True
    net.add(api_item(cid=2, uuid="cccccccc-2222-3333-4444-555555555555", title="Paid", price="5"))
    assert core.lookup_search("fallout4", "paid")[0]["downloadable"] is False
    assert core.is_withdrawn({"deleted": "true"}) and not core.is_withdrawn({"deleted": "false", "restricted": 0, "blocklisted": None})


def test_a_free_creation_without_a_pc_file_is_not_downloadable_in_the_detail(net):
    item = api_item()
    item["download"] = []
    net.add(item)
    one = core.lookup_detail("fallout4", 743724)
    assert one["free"] is True and one["downloadable"] is False and "No PC download" in one["get_in_game_reason"]


def test_author_verified_is_passed_on(net):
    net.add(api_item(author_verified=True))
    assert core.lookup_detail("fallout4", 743724)["author_verified"] is True
    net.add(api_item())
    assert core.lookup_detail("fallout4", 743724)["author_verified"] is False


def test_required_creations_say_whether_they_can_be_downloaded(net):
    net.add(api_item(required_mods=[1001, 1002, 1003]))
    net.add(api_item(cid=1001, uuid="aaaaaaaa-2222-3333-4444-555555555555", title="Free Lib"))
    net.add(api_item(cid=1002, uuid="bbbbbbbb-2222-3333-4444-555555555555", title="Gone Lib", deleted=True))
    net.add(api_item(cid=1003, uuid="cccccccc-2222-3333-4444-555555555555", title="Paid Lib", price="1"))
    got = {m["content_id"]: (m["free"], m["downloadable"]) for m in core.lookup_detail("fallout4", 743724)["required_mods"]}
    assert got == {1001: (True, True), 1002: (True, False), 1003: (False, False)}


def test_pc_release_notes_are_windows_only_cleaned_and_capped():
    notes = [{"hardware_platform": "XBOXONE", "release_notes": [{"note": "console only", "version_name": "9"}]},
             {"hardware_platform": "WINDOWS", "release_notes": [
                 {"note": "Fixed &amp; tweaked<br>things‮", "version_name": "1.05"},
                 {"note": "   ", "version_name": "1.04"}, {"version_name": "1.03"}, "A bare string", 7, None,
                 {"notes": "other key"}]}]
    assert core.pc_release_notes({"release_notes": notes}) == [
        {"version": "1.05", "note": "Fixed & tweaked\nthings"}, {"version": "", "note": "A bare string"},
        {"version": "", "note": "other key"}]
    many = [{"hardware_platform": "WINDOWS", "release_notes": [{"note": f"n{i}", "version_name": str(i)} for i in range(50)]}]
    assert len(core.pc_release_notes({"release_notes": many})) == 20
    for odd in (None, "x", {}, [None, 3, {"hardware_platform": "WINDOWS"}, {"hardware_platform": "WINDOWS", "release_notes": "x"}]):
        assert core.pc_release_notes({"release_notes": odd}) == []
    assert core.pc_release_notes({}) == []


def test_lookup_detail_carries_the_pc_release_notes(net):
    net.add(api_item(release_notes=[{"hardware_platform": "WINDOWS", "release_notes": [{"note": "Hello", "version_name": "1.04"}]}]))
    assert core.lookup_detail("fallout4", 743724)["release_notes"] == [{"version": "1.04", "note": "Hello"}]
    net.add(api_item())
    assert core.lookup_detail("fallout4", 743724)["release_notes"] == []


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
KEY_PATH = "public/content/FALLOUT4/743724/CLASSIFICATION_PREVIEW_IMAGE/a b.png"
KEY_URL_PATH = "/public/content/FALLOUT4/743724/CLASSIFICATION_PREVIEW_IMAGE/a%20b.png"  # as the request path reads


def media(**kw):
    return {"preview_image": {"s3bucket": "ugcmods.bethesda.net", "s3key": KEY_PATH, "file_type": "png", "width": 640, "height": 360, **kw}}


def test_the_preview_is_fetched_from_the_download_host_without_the_key(net):
    net.files[KEY_URL_PATH] = PNG
    assert core.preview_image_bytes(api_item(**media())) == PNG
    url, headers = net.calls[-1]
    assert url == "https://ugcmods.bethesda.net/public/content/FALLOUT4/743724/CLASSIFICATION_PREVIEW_IMAGE/a%20b.png"
    assert "x-bnet-key" not in headers and headers["user-agent"] == core.USER_AGENT and "?" not in url


def test_the_detail_offers_the_reduced_preview_and_it_can_be_fetched_from_it(net):
    net.add(api_item(**media()))
    net.files[KEY_URL_PATH] = PNG
    one = core.lookup_detail("fallout4", 743724)
    assert one["preview"] == {"file_type": "png", "s3key": KEY_PATH}
    secrets_absent(one)
    assert core.preview_image_bytes(one) == PNG
    net.add(api_item())
    assert core.lookup_detail("fallout4", 743724)["preview"] is None


@pytest.mark.parametrize("bad", [
    {"s3bucket": "evil.example.com"}, {"s3bucket": "ugcmods.bethesda.net.evil.com"}, {"s3bucket": None},
    {"s3key": "../etc/passwd"}, {"s3key": "public/../x.png"}, {"s3key": "/public/x.png"}, {"s3key": "public//x.png"},
    {"s3key": "public/x.png?sig=1"}, {"s3key": "public/x.png#a"}, {"s3key": "public\\x.png"}, {"s3key": "public/x\n.png"},
    {"s3key": ""}, {"s3key": None}, {"s3key": 5}, {"s3key": "public/" + "a" * 600},
    {"file_type": "svg"}, {"file_type": "gif"}, {"file_type": None}, {"width": 20000}, {"height": -1}, {"width": "640"}])
def test_an_unsafe_preview_description_is_ignored_without_any_request(net, bad):
    assert core.preview_image_bytes(api_item(**media(**bad))) is None
    assert not net.calls


def test_preview_file_types_are_normalised_and_checked_against_the_bytes(net):
    net.files[KEY_URL_PATH] = PNG
    assert core.preview_image_bytes(api_item(**media(file_type="PNG"))) == PNG
    assert core.preview_image_bytes(api_item(**media(file_type="image/png"))) == PNG
    assert core.preview_image_bytes(api_item(**media(file_type=".png"))) == PNG
    assert core.preview_image_bytes(api_item(**media(file_type="jpg"))) is None  # PNG bytes are not a JPEG
    net.files[KEY_URL_PATH] = b"\xff\xd8\xff\xe0" + b"j" * 20
    assert core.preview_image_bytes(api_item(**media(file_type="jpeg"))) is not None
    net.files[KEY_URL_PATH] = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    assert core.preview_image_bytes(api_item(**media(file_type="webp"))) is not None
    net.files[KEY_URL_PATH] = b"RIFF\x00\x00\x00\x00WAVEfmt "
    assert core.preview_image_bytes(api_item(**media(file_type="webp"))) is None
    net.files[KEY_URL_PATH] = b"<html>not an image</html>"
    assert core.preview_image_bytes(api_item(**media())) is None


def test_the_preview_has_a_size_cap_and_never_raises(net, monkeypatch):
    net.files[KEY_URL_PATH] = PNG + b"x" * 100
    assert core.preview_image_bytes(api_item(**media()), max_bytes=len(PNG) + 99) is None
    assert core.preview_image_bytes(api_item(**media()), max_bytes=len(PNG) + 100) is not None
    assert core.PREVIEW_MAX_BYTES == 5 * 1024 ** 2
    stream = Endless()
    monkeypatch.setattr(core, "_open", lambda request, timeout: stream)
    assert core.preview_image_bytes(api_item(**media()), max_bytes=1000) is None and stream.total <= 1001
    for failure in NET_FAILURES + [core.CreationsError("CREATIONS_ERROR", "refused a redirect"), KeyError("x"), RuntimeError("odd")]:
        def failing(request, timeout, failure=failure):
            raise failure
        monkeypatch.setattr(core, "_open", failing)
        assert core.preview_image_bytes(api_item(**media())) is None
    for junk in (None, {}, {"preview_image": "x"}, {"preview": 5}, 5, "x"):
        assert core.preview_image_bytes(junk) is None


def test_the_preview_request_is_made_through_the_refusing_seam(monkeypatch):
    built = []

    class Opener:
        def open(self, request, timeout):
            return Response(PNG)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: (built.append(handlers), Opener())[1])
    monkeypatch.setattr(core, "_open", REAL_OPEN)
    assert core.preview_image_bytes(api_item(**media())) == PNG
    assert built == [(core._NoRedirect,)]


def test_local_statuses_match_local_status_for_many_creations_in_one_pass(net, tmp_path):
    dl = tmp_path / "dl"
    a = fetched(net, tmp_path, cid=743724, version="1.04")
    b = fetched(net, tmp_path, cid=800, version="2.0", title="Other")
    fetched(net, tmp_path, game="skyrimspecialedition", cid=900, version="1.0", ctime=300, title="Sky")
    got = core.local_statuses(dl, "fallout4", {743724: "1.04", 800: "3.0", 12345: None, "900": None})
    assert got[743724] == core.local_status(dl, "fallout4", 743724, "1.04") == {
        "state": "downloaded", "archive": str(a), "version": "1.04", "meta_installed": False}
    assert got[800] == {"state": "update", "archive": str(b), "version": "2.0", "meta_installed": False}
    assert got[12345]["state"] == "none" and got[900]["state"] == "none"  # 900 is a Skyrim download
    assert core.local_statuses(tmp_path / "missing", "fallout4", {1: None}) == {1: core.local_status(tmp_path / "missing", "fallout4", 1, None)}


# ---------------------------------------------------------------- review fixes: file name, .meta, errors, small items

# MO2 2.5.2, nexusinterface.cpp, NexusInterface::interpretNexusFileName (copied verbatim; group 3 is the Nexus mod id)
MO2_COMPLEX = re.compile(r"""^([a-zA-Z0-9_'"\-.() ]*?)([-_ ][VvRr]+[0-9]+(?:(?:[\.][0-9]+){0,2}|(?:[_][0-9]+){0,2}|(?:[-.][0-9]+){0,2})?[ab]?)??-([1-9][0-9]+)?-.*?\.(zip|rar|7z)""")

TITLES = ["Fine Mod", "Fine Mod: Revamped", "Swords, Shields and More", "Arcade; Pack", "Show Prompts & Fixes", "Pre-war Rifles",
          "Pre-war and Post-war", "Mod -123- Pack", "Mod-123-Pack", "Mod - 123 - Pack", "A--B", "x-1234-y", "2077-123-4", "-5-", "--",
          "Vol-2-Pack-3", "Pack - 99", "Armour-1-2-3-4-5", "caf\u00e9 \u00e9p\u00e9e", "Fallout-4-Nuka-1000-Rebuild", "Mod-1-", "-1-Mod",
          "Mod -0- x", "Mod-01-x", "Mod-1234567890-x", "Hyphen-", "-Hyphen", "a-b-c-d", "Fine Mod v1.2-77-final"]
VERSIONS = ["1.04", "1", "1.0-beta", "1.0-2-3", "2-5-", "-7-", "--", "v1.2", "V2", "1.0 beta", "1,0", "1;0", "", "0", "3-14-15", "1.0-12"]


def test_mo2_reads_a_nexus_id_from_the_old_name_but_from_none_of_the_new_ones():
    old = MO2_COMPLEX.search("Fine Mod-743724-1.04.zip")
    assert old is not None and old.group(3) == "743724"  # the regex copy is faithful, and the old name was a real problem
    for title in TITLES:
        for version in VERSIONS:
            name = core.zip_name(title, 743724, version)
            assert MO2_COMPLEX.search(name) is None, name
            assert not re.search(r"-[0-9]*-", name), name  # nothing at all that could open the pattern
            assert name.endswith(".zip") and "(Creation 743724)" in name
    # and Qt's own regex engine (the one MO2 really uses) agrees with the Python copy, on the old and the new names
    from PyQt6.QtCore import QRegularExpression
    qt = QRegularExpression(MO2_COMPLEX.pattern)
    assert qt.match("Fine Mod-743724-1.04.zip").captured(3) == "743724"
    for title in TITLES:
        assert not qt.match(core.zip_name(title, 743724, "1.0-2-3")).hasMatch(), title
    # the same holds for every id, including ones that look like versions
    for cid in (1, 12, 743724, 2147483647):
        assert MO2_COMPLEX.search(core.zip_name("Pack-12-x", cid, "1-2-3")) is None


def test_the_downloads_nudge_leaves_nothing_behind_and_never_raises(tmp_path):
    (tmp_path / "keep.zip").write_bytes(b"x")
    core.nudge_downloads_watcher(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.zip"]
    core.nudge_downloads_watcher(tmp_path / "missing")  # no folder: nothing happens, nothing raised


def test_zip_names_keep_readable_hyphens_and_say_the_id_in_words():
    assert core.zip_name("Fine Mod: Revamped", 743724, "1.04") == "Fine Mod_ Revamped (Creation 743724) v1.04.zip"
    assert core.zip_name("Pre-war Rifles", 55, "1.0") == "Pre-war Rifles (Creation 55) v1.0.zip"  # a lone hyphen is harmless
    assert core.zip_name("Mod -123- Pack", 9, "2") == "Mod 123- Pack (Creation 9) v2.zip"
    assert core.zip_name("Show Prompts & Fixes", 7, "1.0") == "Show Prompts & Fixes (Creation 7) v1.0.zip"
    assert core.zip_name("Fine Mod", 1, "1.0-beta") == core.zip_name("Fine Mod", 1, "1.0-beta")  # deterministic


def test_zip_names_stay_distinct_when_cleaning_changes_the_version():
    plain = core.zip_name("T", 1, "1.04")
    assert plain == "T (Creation 1) v1.04.zip"
    names = {core.zip_name("T", 1, v) for v in ("1 0", "1_0", "1:0", "1|0", "1.0:", "1.0|", "v1.0", "1.0", "1-2-3", "1_2_3")}
    assert len(names) == 10
    assert core.zip_name("T", 1, "1_0") == "T (Creation 1) v1_0.zip"  # unchanged by cleaning: no hash
    assert core.zip_name("a/b\\c:d", 2, "1") == "a_b_c_d (Creation 2) v1.zip"
    assert core.zip_name("  ..  ", 2, "") == "Creation (Creation 2) v0 [e3b0c4].zip"  # an empty version becomes 0, with the hash of the raw one
    assert core.zip_name("T", 1, "1 0") == core.zip_name("T", 1, "1 0") and "[" in core.zip_name("T", 1, "1 0")


def test_downloads_are_found_by_their_meta_not_by_their_name(net, tmp_path):
    net.add(api_item(title="Mod -123- Pack"))
    p = core.plan("fallout4", 743724)
    got, _, _ = core.fetch(tmp_path / "dl", p)
    assert MO2_COMPLEX.search(got.name) is None
    renamed = got.with_name("renamed by the user.zip")
    got.rename(renamed)
    Path(str(got) + ".meta").rename(Path(str(renamed) + ".meta"))
    assert core.find_downloaded(tmp_path / "dl", "fallout4", 743724, "1.04") == renamed
    assert core.local_status(tmp_path / "dl", "fallout4", 743724, "1.04")["archive"] == str(renamed)


def _plan(title, version="1.04", cid=743724, game="fallout4"):
    return core.Plan(game, cid, UUID, title, "Someone", version, 10, "", "https://ugcmods.bethesda.net/x.ckm")


QSETTINGS_TITLES = ["Swords, Shields and More", "Arcade; Pack", 'Quote "x" and back\\slash', "Swords & Shields, Vol. 2",
                    "Caf\u00e9 \u00c9p\u00e9e \u2013 \u65e5\u672c\u8a9e", "a=b", "@Home", "@@Home", "@Invalid()", "Trailing\\", "C:\\path\\x01 \"q\"",
                    "100% [done] #1", "x;y,z=w", "Don't", "back\\\\double", "tab\there"]


@pytest.mark.parametrize("title", QSETTINGS_TITLES)
def test_the_meta_reads_back_through_qsettings_exactly(title, tmp_path):
    from PyQt6.QtCore import QSettings
    archive = tmp_path / "x.zip"
    version = "1.0, beta; \"2\"" if "," in title else "1.04"
    core.write_meta(archive, _plan(title, version), "0123456789abcdef0123456789abcdef")
    q = QSettings(str(archive) + ".meta", QSettings.Format.IniFormat)
    cleaned, ver = core.clean_text(title), core.clean_text(version)
    for key in ("name", "modName"):
        assert q.value(key) == cleaned, key
    assert q.value("version") == ver and q.value("creationsVersion") == ver
    assert q.value("modID", 0, int) == -1 and q.value("fileID", 0, int) == 0
    assert q.value("gameName") == "Fallout4" and q.value("repository") == "Bethesda.net"
    assert q.value("url") == f"https://creations.bethesda.net/en/fallout4/details/{UUID}"
    assert q.value("creationsContentId") == "743724" and q.value("installed", False, bool) is False
    # and our own reader agrees with Qt's
    ours = core.read_meta(archive)
    assert ours["name"] == ours["modname"] == cleaned and ours["version"] == ver and ours["creationsversion"] == ver


@pytest.mark.parametrize("value", QSETTINGS_TITLES + ["", " lead", "trail ", "a\nb", "x\x01y", "\x01a", "\x01\x01", "\u2028"])
def test_ini_quote_is_what_qts_writer_writes_and_both_readers_undo_it(value, tmp_path):
    from PyQt6.QtCore import QSettings
    path = tmp_path / "qt.ini"
    q = QSettings(str(path), QSettings.Format.IniFormat)
    q.setValue("k", value)
    q.sync()
    del q
    written = next(l for l in re.split(r'\r?\n', path.read_bytes().decode("utf-8-sig")) if l.startswith("k=")).split("=", 1)[1]
    assert core.ini_quote(value) == written
    assert core.ini_unquote(written) == value
    ours = tmp_path / "ours.ini"
    ours.write_bytes(("[General]\r\nk=" + core.ini_quote(value) + "\r\n").encode("utf-8"))
    assert QSettings(str(ours), QSettings.Format.IniFormat).value("k", "") == value


def test_a_meta_that_mo2_rewrote_with_qsettings_still_reads(net, tmp_path):
    from PyQt6.QtCore import QSettings
    net.add(api_item(title="Swords, Shields and More; Vol. 2"))
    got, _, _ = core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    q = QSettings(str(got) + ".meta", QSettings.Format.IniFormat)  # MO2 marks the download installed this way
    q.setValue("installed", True)
    q.setValue("category", 0)
    q.sync()
    del q
    meta = core.read_meta(got)
    assert meta["installed"] == "true" and meta["name"] == "Swords, Shields and More; Vol. 2" and meta["creationsversion"] == "1.04"
    assert core.local_status(tmp_path / "dl", "fallout4", 743724, "1.04")["meta_installed"] is True


def test_the_meta_keeps_clean_texts_line_injection_protection(tmp_path):
    archive = tmp_path / "x.zip"
    core.write_meta(archive, _plan("A\nB=1\r\n[Evil]\u2028C", "1\n2"), "00")
    lines = Path(str(archive) + ".meta").read_bytes().decode("utf-8-sig").split("\r\n")
    assert [l for l in lines if l.startswith("[")] == ["[General]"] and not any(l.startswith(("B=", "C=")) for l in lines)
    assert core.read_meta(archive)["name"] == "A B=1 [Evil] C"


def test_a_meta_that_exists_keeps_mo2s_state_flags(net, tmp_path):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    dl = tmp_path / "dl"
    got, _, _ = core.fetch(dl, p)
    meta = Path(str(got) + ".meta")
    # the .meta lost our keys (and MO2 had set its flags): the same bytes are accepted and the flags survive
    meta.write_text("[General]\r\ninstalled=true\r\nuninstalled=true\r\nremoved=true\r\npaused=true\r\nuserData=@Variant()\r\n", encoding="utf-8")
    same, _, reused = core.fetch(dl, p)
    assert same == got and not reused
    m = core.read_meta(got)
    assert (m["installed"], m["uninstalled"], m["removed"], m["paused"]) == ("true",) * 4
    assert m["creationscontentid"] == "743724" and m["creationsversion"] == "1.04" and m["modid"] == "-1"
    meta.write_text("[General]\r\ninstalled=maybe\r\n", encoding="utf-8")
    core.fetch(dl, p)
    assert core.read_meta(got)["installed"] == "false"  # only true or false is carried over


def _slow_convert(monkeypatch, after):
    real = core.ckm_to_zip

    def convert(ckm, dest):
        result = real(ckm, dest)
        after(dest)
        return result

    monkeypatch.setattr(core, "ckm_to_zip", convert)


def test_a_cancel_just_before_the_save_leaves_nothing(net, tmp_path, monkeypatch):
    net.add(api_item())
    flag = [False]
    _slow_convert(monkeypatch, lambda dest: flag.__setitem__(0, True))
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", core.plan("fallout4", 743724), cancel=lambda: flag[0])
    assert exc.value.status == "CANCELLED" and list((tmp_path / "dl").iterdir()) == []


def test_the_zip_is_moved_into_place_without_ever_replacing_a_file(net, tmp_path, monkeypatch):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    target = tmp_path / "dl" / core.zip_name(p.title, p.content_id, p.version)
    # another file appears with this name after the check a replace would have relied on
    _slow_convert(monkeypatch, lambda dest: target.write_bytes(b"someone else's"))
    with pytest.raises(core.CreationsError) as exc:
        core.fetch(tmp_path / "dl", p)
    assert exc.value.status == "TARGET_EXISTS" and target.read_bytes() == b"someone else's"
    assert not list((tmp_path / "dl").glob("*.part")) and not Path(str(target) + ".meta").exists()
    # the same bytes appearing meanwhile are accepted
    target.unlink()
    _slow_convert(monkeypatch, lambda dest: target.write_bytes(dest.read_bytes()))
    got, _, reused = core.fetch(tmp_path / "dl", p)
    assert got == target and not reused and Path(str(target) + ".meta").is_file()


def test_download_errors_name_only_the_kind_of_failure_never_the_signed_link(net, tmp_path, monkeypatch):
    net.add(api_item())
    p = core.plan("fallout4", 743724)
    secret = "https://ugcmods.bethesda.net/files/x.ckm?sig=SIGNED-SECRET&exp=1"
    failures = [http.client.InvalidURL(f"URL can't contain control characters. {secret!r} (found at least ' ')"),
                OSError(f"boom {secret}"), TimeoutError(secret), ConnectionResetError(secret), ssl.SSLError(secret),
                urllib.error.URLError(OSError(f"inner {secret}")), http.client.IncompleteRead(b"x", 5)]
    for failure in failures:
        def fail(request, timeout, failure=failure):
            raise failure
        monkeypatch.setattr(core, "_open", fail)
        with pytest.raises(core.CreationsError) as exc:
            core.fetch(tmp_path / "dl", p)
        text = str(exc.value)
        assert text.startswith("download failed: ") and "SIGNED-SECRET" not in text and "sig=" not in text and "?" not in text, text
    assert "InvalidURL, malformed link" in str(_download_error(monkeypatch, p, failures[0]))
    assert "timed out" in str(_download_error(monkeypatch, p, failures[2]))
    # and a failure in the middle of the stream
    class Breaks(Response):
        def read(self, n=-1):
            raise OSError(f"reset {secret}")

    monkeypatch.setattr(core, "_open", lambda request, timeout: Breaks(b""))
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(p.url, tmp_path / "x.ckm", None, "")
    assert "SIGNED-SECRET" not in str(exc.value)


def _download_error(monkeypatch, p, failure):
    def fail(request, timeout):
        raise failure
    monkeypatch.setattr(core, "_open", fail)
    with pytest.raises(core.CreationsError) as exc:
        core.download_ckm(p.url, Path("unused.ckm"), None, "")
    return exc.value


def test_the_download_socket_timeout_is_short(net, tmp_path, monkeypatch):
    net.add(api_item())
    seen = []
    real = core._open
    monkeypatch.setattr(core, "_open", lambda request, timeout: (seen.append(timeout), real(request, timeout))[1])
    core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    assert core.DOWNLOAD_SOCKET_TIMEOUT == 30 and seen[-1] == 30


@pytest.mark.parametrize("bad", ["a b", "a\tb", "a\nb", "a\x00b", "a\x7fb", " lead", "trail ", "x\u00a0y"])
def test_a_download_link_with_whitespace_or_control_characters_is_refused(bad):
    item = api_item()
    meta = item["download"][0]["published"][0]["client"]["main"]
    meta["download_url"] = f"{HOST}/files/743724/{bad}/743724.ckm?sig=SIGNED-SECRET"
    with pytest.raises(core.CreationsError) as exc:
        core.pc_files(item)
    assert exc.value.status == "UNEXPECTED_HOST" and "SIGNED-SECRET" not in str(exc.value)
    assert core._summary("fallout4", item)["version"] is None  # a summary just has no PC file


def test_scrub_removes_the_key_and_signed_links_whatever_surrounds_them():
    core._key_cache[:] = ["PUBLIC-CLIENT-KEY-1234567890abcdef"]
    assert "PUBLIC-CLIENT-KEY" not in core.scrub("key PUBLIC-CLIENT-KEY-1234567890abcdef inside")
    for text in ("bad https://ugcmods.bethesda.net/files/a b/c.ckm?sig=SIGNED-SECRET x y z",
                 "'/files/x.ckm?sig=SIGNED SECRET with spaces' (found at least ' ')",
                 "multi\nline ?sig=SIGNED-SECRET\nmore SIGNED-SECRET2",
                 "headers x-bnet-key: PUBLIC-SOMETHING-ELSE end",
                 "https://ugcmods.bethesda.net/files/SIGNED-SECRET/x.ckm"):
        out = core.scrub(text)
        assert "SIGNED-SECRET" not in out and "SIGNED SECRET" not in out and "SECRET2" not in out and "SOMETHING-ELSE" not in out, out
    assert core.scrub("plain words") == "plain words"
    assert len(core.scrub("x" * 100000)) < 5000


def test_core_log_lines_are_whole_messages_without_args(caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="creations_browser")
    core.log_line(logging.INFO, "got https://ugcmods.bethesda.net/a?sig=SIGNED-SECRET %s %d")  # a stray % must not break it
    try:
        raise ValueError("boom ?sig=SIGNED-SECRET")
    except ValueError:
        core.log_unexpected("it failed")
    assert all(not r.args for r in caplog.records)
    assert "SIGNED-SECRET" not in caplog.text and "Traceback" in caplog.text and "ValueError" in caplog.text and "it failed" in caplog.text


def test_a_raw_item_cannot_use_the_reduced_preview_form(net):
    net.add(api_item(**media()))
    net.files[KEY_URL_PATH] = PNG
    one = core.lookup_detail("fallout4", 743724)
    assert core.preview_image_bytes(one) == PNG
    count = len(net.calls)
    # a plain dict in "preview" (what a catalogue could say) is never trusted, with or without a preview_image beside it
    plain = {"file_type": "png", "s3key": KEY_PATH}
    assert core.preview_image_bytes({"preview": plain}) is None
    assert core.preview_image_bytes({"preview": plain, "preview_image": "x"}) is None
    assert core.preview_image_bytes({"preview": dict(one["preview"])}) is None
    assert core.preview_image_bytes({"preview": plain, "preview_image": {"s3bucket": "evil.example.com", "s3key": KEY_PATH, "file_type": "png"}}) is None
    assert len(net.calls) == count  # none of them made a request
    # the raw form still needs the bucket
    assert core.preview_image_bytes({"preview_image": {"s3key": KEY_PATH, "file_type": "png"}}) is None
    assert core.preview_image_bytes(api_item(**media())) == PNG


def test_identical_release_notes_are_listed_once_in_order():
    win = lambda *entries: {"hardware_platform": "WINDOWS", "release_notes": list(entries)}  # noqa: E731
    a, b = {"note": "Fixed A", "version_name": "1.0"}, {"note": "Fixed B", "version_name": "1.0"}
    notes = [win(a, b, dict(a)), win(dict(b), {"note": "Fixed A", "version_name": "1.1"}, "Fixed A", "Fixed A")]
    assert core.pc_release_notes({"release_notes": notes}) == [
        {"version": "1.0", "note": "Fixed A"}, {"version": "1.0", "note": "Fixed B"},
        {"version": "1.1", "note": "Fixed A"}, {"version": "", "note": "Fixed A"}]
    many = [win(*[{"note": f"n{i % 30}", "version_name": "1"} for i in range(200)])]
    assert len(core.pc_release_notes({"release_notes": many})) == 20  # the cap counts distinct entries


def test_console_only_creations_are_not_downloadable_in_a_search(net):
    net.add(api_item(cid=1, uuid="11111111-2222-3333-4444-000000000001", title="Console Thing", hardware_platforms=["XBOXONE", "XBOXSERIESX"]))
    net.add(api_item(cid=2, uuid="11111111-2222-3333-4444-000000000002", title="Both Thing", hardware_platforms=["XBOXONE", "windows"]))
    net.add(api_item(cid=3, uuid="11111111-2222-3333-4444-000000000003", title="Unknown Thing", hardware_platforms=[]))
    net.add(api_item(cid=4, uuid="11111111-2222-3333-4444-000000000004", title="Missing Thing"))
    del net.items[4]["hardware_platforms"]
    net.add(api_item(cid=5, uuid="11111111-2222-3333-4444-000000000005", title="Paid Console Thing", price="5", hardware_platforms=["PS4"]))
    net.add(api_item(cid=6, uuid="11111111-2222-3333-4444-000000000006", title="Gone Console Thing", deleted=True, hardware_platforms=["PS4"]))
    got = {r["title"]: r for r in core.lookup_search("skyrimspecialedition", "thing")}
    console = got["Console Thing"]
    assert console["downloadable"] is False and console["get_in_game_reason"] == "No PC version: not available here"
    assert console["free"] is True and console["platforms"] == ["XBOXONE", "XBOXSERIESX"]
    assert texts.marker(console) == "No PC version"
    for title in ("Both Thing", "Unknown Thing", "Missing Thing"):
        assert got[title]["downloadable"] is True and got[title]["get_in_game_reason"] is None, title
    assert got["Paid Console Thing"]["get_in_game_reason"] == "Paid Creation: get it in game" and not got["Paid Console Thing"]["downloadable"]
    assert texts.marker(got["Paid Console Thing"]) == "Get it in game"
    assert got["Gone Console Thing"]["get_in_game_reason"] == core.WITHDRAWN_REASON and texts.marker(got["Gone Console Thing"]) == "Withdrawn"
    assert texts.marker(got["Both Thing"]) == "Free"
    # the detail agrees with the search
    assert core.lookup_detail("fallout4", 1)["downloadable"] is False
    assert core.lookup_detail("fallout4", 1)["get_in_game_reason"] == "No PC version: not available here"


# ---------------------------------------------------------------- search, sort and filters (1.1.0)

def uid(i):
    return f"{i:08x}-2222-3333-4444-555555555555"


def bulk(net, n, prefix="College", start=1, **extra):
    """``n`` catalogue items (search results only: no downloads registered) with ids ``start``..."""
    for i in range(start, start + n):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"{prefix} {i:04d}", **extra)


def content_params(net):
    """The query of every ``content`` request so far, as dicts."""
    return [{k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).items()}
            for u, _ in net.calls if urllib.parse.urlsplit(u).path.endswith("/ugcmods/v1/content")]


def test_stats_helpers_read_the_catalogue_defensively():
    from fakes import make_stats
    item = {"stats": make_stats(pc=41500, total=52100, likes=120, subs=900, rating=2), "ptime": 1741392000, "utime": 1741478400,
            "categories": ["Armor", " Quests ", 5, "x" * 200, ""]}
    assert core.pc_downloads(item) == 41500 and core.all_downloads(item) == 52100
    assert core.like_count(item) == 120 and core.subscribe_count(item) == 900 and core.rating_level(item) == 2
    assert core.published_time(item) == 1741392000 and core.updated_time(item) == 1741478400
    assert core.category_names(item) == ["Armor", "Quests"]
    # no PC block: the PC figure is None; the all-platform figure is unaffected
    console = {"stats": make_stats(pc=None, total=77)}
    assert core.pc_downloads(console) is None and core.all_downloads(console) == 77
    # the ALL block stands in when totals is missing; the platform key is matched ignoring case
    odd = {"stats": {"platforms": {"all": {"downloads": "12", "likes": 3.0}, "windows": {"downloads": 4}}}}
    assert core.pc_downloads(odd) == 4 and core.all_downloads(odd) == 12 and core.like_count(odd) == 3
    # nothing usable never raises: every helper answers None (or empty)
    for bad in ({}, {"stats": None}, {"stats": []}, {"stats": {"platforms": [], "totals": "x"}},
                {"stats": {"platforms": {"WINDOWS": None}, "totals": {"downloads": -1, "likes": True, "rating": 7}}},
                {"stats": {"totals": {"downloads": float("nan"), "likes": "many", "subscribes": [1]}}},
                {"ptime": "soon", "utime": 0, "first_ptime": None, "categories": "Armor"}):
        assert core.pc_downloads(bad) is None and core.all_downloads(bad) is None and core.like_count(bad) is None
        assert core.subscribe_count(bad) is None and core.rating_level(bad) is None
        assert core.published_time(bad) is None and core.updated_time(bad) is None and core.category_names(bad) == []
    assert core.published_time({"ptime": 0, "first_ptime": 1690000000}) == 1690000000  # the first publication stands in
    assert core.updated_time({"utime": 1741478400000}) == 1741478400  # milliseconds are folded down
    assert [core.rating_level({"stats": {"totals": {"rating": r}}}) for r in (0, 1, 2, "HIGH", "medium", "low", 3, None, "bad")] == [
        0, 1, 2, 2, 1, 0, None, None, None]


def test_summaries_carry_the_stats_as_plain_numbers(net):
    from fakes import make_stats
    net.add(api_item(stats=make_stats(pc=500, total=900, likes=7, subs=40, rating=1), categories=["Armor", "Quests"]))
    for got in (core.lookup_search("fallout4", "fine")[0], core.lookup_detail("fallout4", 743724)):
        assert (got["downloads"], got["downloads_pc"], got["downloads_all"], got["likes"], got["subscribes"], got["rating"]) == (
            900, 500, 900, 7, 40, 1)  # "downloads" is the all-platform total, which rows show and the sort uses
        assert got["published"] == 1700000000 and got["updated"] == 1710000000 and got["categories"] == ["Armor", "Quests"]
        secrets_absent(got)


def test_search_page_sends_only_checked_values_and_returns_the_total(net):
    bulk(net, 7)
    items, total = core.search_page("skyrimspecialedition", "college", page=2, size=3, sort="utime", order="desc",
                                    time_period="weekly", platforms="WINDOWS", marketplace=False, category="Armor", author="Someone")
    assert total == 0 and items == []  # the fake items are not in the Armor category
    (q,) = content_params(net)
    assert q == {"product": "skyrim", "page": "2", "size": "3", "text": "college", "sort": "utime", "order": "desc",
                 "time_period": "weekly", "hardware_platforms": "WINDOWS", "marketplace": "false", "categories": "Armor",
                 "author_displayname": "Someone"}
    net.calls.clear()
    items, total = core.search_page("fallout4", page=2, size=3)  # nothing asked for but the paging
    assert content_params(net) == [{"product": "fallout4", "page": "2", "size": "3"}]
    assert total == 7 and [i["title"] for i in items] == ["College 0004", "College 0005", "College 0006"]
    assert core.lookup_page("fallout4", None, page=1, size=2)["total"] == 7
    assert core.lookup_page("fallout4", "  ", page=1, size=2)["rows"][0]["title"] == "College 0001"  # blank words are no words


@pytest.mark.parametrize("bad", [
    {"page": 0}, {"page": 1001}, {"page": "1"}, {"page": True}, {"size": 0}, {"size": 101}, {"size": 2.5}, {"size": None},
    {"sort": "downloads"}, {"sort": "likes"}, {"sort": "popular"}, {"sort": "title; drop"}, {"order": "up"}, {"order": "DESC"},
    {"time_period": "yearly"}, {"time_period": "all-time"}, {"platforms": "PS4"}, {"platforms": "windows"}, {"platforms": True},
    {"marketplace": True}, {"marketplace": "false"}, {"marketplace": 0},
    {"category": ""}, {"category": " Armor"}, {"category": "x" * 81}, {"category": "Ar\nmor"}, {"category": "Ar‮mor"}, {"category": 5},
    {"author": ""}, {"author": "a\x00b"}, {"author": "x" * 81}, {"author": ["a"]}])
def test_values_outside_the_allow_lists_are_refused_before_anything_is_sent(net, bad):
    with pytest.raises(core.CreationsError) as exc:
        core.search_page("fallout4", "college", **bad)
    assert exc.value.status == "INVALID_ARGUMENT" and net.calls == []


def test_search_text_is_length_limited_and_a_bad_game_is_refused(net):
    for args in (("fallout4", "x" * 201), ("fallout4", 5), ("oblivion", "x")):
        with pytest.raises(core.CreationsError) as exc:
            core.search_page(*args)
        assert exc.value.status == "INVALID_ARGUMENT"
    assert net.calls == []
    assert core.valid_name("Armor") and core.valid_name("Q&A Things") and not core.valid_name("") and not core.valid_name(None)
    assert core.SORT_KEYS == {"subscriptions", "ptime", "utime", "ctime", "title", "rating"}
    assert core.TIME_PERIODS == {"all_time", "monthly", "weekly", "daily"} and core.PLATFORM_FILTERS == {"WINDOWS"}


def test_categories_are_cleaned_sorted_and_read_in_either_shape(net):
    net.category_names["fallout4"] = ["Weapons", "armor", "Armor", "Quests &amp; More", "x" * 90, "", "Zed"]
    assert core.categories("fallout4") == ["armor", "Quests & More", "Weapons", "Zed"]
    assert content_params(net) == [] and any("/categories?product=fallout4" in u for u, _ in net.calls)
    net.categories_wrapped = True
    assert core.categories("skyrimspecialedition") == ["Armor", "Quests"]
    net.categories_fail = True
    with pytest.raises(core.CreationsError) as exc:
        core.categories("fallout4")
    assert exc.value.status == "CREATIONS_ERROR"


def test_a_categories_answer_of_the_wrong_shape_is_an_error(net, monkeypatch):
    monkeypatch.setattr(core, "_api_raw", lambda *a, **k: {"categories": "none"})
    with pytest.raises(core.CreationsError, match="unexpected shape"):
        core.categories("fallout4")
    monkeypatch.setattr(core, "_api_raw", lambda *a, **k: ["x"])
    with pytest.raises(core.CreationsError, match="unexpected shape"):
        core.categories("fallout4")


def test_lookup_collect_reads_pages_of_100_one_after_the_other(net):
    bulk(net, 250)
    got = core.lookup_collect("fallout4", "college")
    assert len(got["rows"]) == 250 and got["total"] == 250 and got["capped"] is False
    assert [(q["page"], q["size"]) for q in content_params(net)] == [("1", "100"), ("2", "100"), ("3", "100")]
    assert got["rows"][0]["title"] == "College 0001" and got["rows"][-1]["title"] == "College 0250"  # Bethesda's order kept
    net.calls.clear()
    bulk(net, 40, prefix="Few", start=1000)
    assert len(core.lookup_collect("fallout4", "few")["rows"]) == 40 and len(content_params(net)) == 1


def test_lookup_collect_stops_at_the_cap_with_at_most_five_requests(net):
    bulk(net, 650)
    got = core.lookup_collect("fallout4", "college")
    assert len(got["rows"]) == core.TEXT_CAP == 500 and got["total"] == 650 and got["capped"] is True
    assert [q["page"] for q in content_params(net)] == ["1", "2", "3", "4", "5"]
    net.calls.clear()
    bulk(net, 500, prefix="Exact", start=2000)
    exact = core.lookup_collect("fallout4", "exact")
    assert len(exact["rows"]) == 500 and exact["capped"] is False and len(content_params(net)) == 5
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_collect("fallout4", "college", cap=501)
    assert exc.value.status == "INVALID_ARGUMENT"


def test_lookup_collect_passes_the_filters_and_can_be_cancelled(net):
    bulk(net, 300, categories=["Quests"])
    asked = []

    def cancel():
        asked.append(1)
        return len(asked) > 1  # the first request goes, the second does not

    with pytest.raises(core.CreationsError) as exc:
        core.lookup_collect("fallout4", "college", platforms="WINDOWS", marketplace=False, category="Quests", cancel=cancel)
    assert exc.value.status == "CANCELLED" and len(content_params(net)) == 1
    (q,) = content_params(net)
    assert q["hardware_platforms"] == "WINDOWS" and q["marketplace"] == "false" and q["categories"] == "Quests"
    with pytest.raises(core.CreationsError):
        core.lookup_collect("fallout4", "   ")


def test_lookup_browse_collect_reads_up_to_a_thousand_in_ten_requests(net):
    from fakes import make_stats
    for i in range(1, 1251):  # popularity order = id order; the most downloaded one sits at position 517
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"Thing {i:04d}",
                                stats=make_stats(pc=5, total=9_000_000 if i == 517 else i, subs=100_000 - i))
    got = core.lookup_browse_collect("fallout4", platforms="WINDOWS", marketplace=False)
    assert len(got["rows"]) == core.BROWSE_CAP == 1000 and got["total"] == 1250 and got["capped"] is True
    calls = content_params(net)
    assert [q["page"] for q in calls] == [str(n) for n in range(1, 11)] and {q["size"] for q in calls} == {"100"}
    assert {(q["sort"], q["order"], q["time_period"]) for q in calls} == {("subscriptions", "desc", "all_time")}
    assert all(q["hardware_platforms"] == "WINDOWS" and q["marketplace"] == "false" and "text" not in q for q in calls)
    assert got["rows"][516]["title"] == "Thing 0517" and got["rows"][516]["downloads"] == 9_000_000
    assert len({r["content_id"] for r in got["rows"]}) == 1000
    net.calls.clear()
    small = core.lookup_browse_collect("fallout4", cap=250, sort="ptime", order="desc", time_period=None)
    assert len(small["rows"]) == 250 and [q["page"] for q in content_params(net)] == ["1", "2", "3"]
    assert all("time_period" not in q and q["sort"] == "ptime" for q in content_params(net))
    for bad in ({"cap": 1001}, {"cap": 0}, {"sort": "downloads"}, {"time_period": "yearly"}, {"order": "up"}):
        with pytest.raises(core.CreationsError) as exc:
            core.lookup_browse_collect("fallout4", **bad)
        assert exc.value.status == "INVALID_ARGUMENT"


def test_lookup_browse_collect_stops_when_the_catalogue_runs_out_and_reports_progress(net):
    bulk(net, 130)
    seen = []
    got = core.lookup_browse_collect("fallout4", progress=lambda done, expected: seen.append((done, expected)))
    assert len(got["rows"]) == 130 and got["capped"] is False and len(content_params(net)) == 2
    assert seen == [(1, 2), (2, 2)]  # pages read, of the pages the catalogue's total needs (at most ten)
    net.calls.clear()
    bulk(net, 1500, prefix="Many", start=1000)
    seen.clear()
    core.lookup_browse_collect("fallout4", progress=lambda done, expected: seen.append((done, expected)))
    assert seen[0] == (1, 10) and seen[-1] == (10, 10) and len(seen) == 10


def test_lookup_browse_collect_can_be_cancelled_between_requests(net):
    bulk(net, 500)
    asked = []

    def cancel():
        asked.append(1)
        return len(asked) > 2  # two requests go, the third does not

    with pytest.raises(core.CreationsError) as exc:
        core.lookup_browse_collect("fallout4", cancel=cancel)
    assert exc.value.status == "CANCELLED" and len(content_params(net)) == 2


def test_a_summary_carries_the_creator_marks(net):
    verified = core._summary("fallout4", api_item(author_verified=True, author_official=True))
    assert verified["author_verified"] is True and verified["author_official"] is True
    plain = core._summary("fallout4", api_item(author_verified="false"))
    assert plain["author_verified"] is False and plain["author_official"] is False


def test_every_sort_choice_is_known_and_the_server_ones_are_allow_listed():
    assert core.SORT_MODES == ("match", "downloads", "weekly", "monthly", "popular", "likes", "rating", "newest", "updated", "title")
    for key, (sort, order, period) in core.SERVER_SORTS.items():
        assert sort in core.SORT_KEYS and order in core.ORDERS and (period is None or period in core.TIME_PERIODS), key
    assert {core.SERVER_SORTS[k][2] for k in core.WORDLESS_SORTS} == {"weekly", "monthly", "all_time"}
    assert core.POPULARITY_ORDER == ("subscriptions", "desc", "all_time")
    assert not set(core.LOCAL_SORTS) & set(core.SERVER_SORTS) and set(core.SORT_MODES) == (
        set(core.SERVER_SORTS) | set(core.LOCAL_SORTS) | set(core.TEXT_ONLY_SORTS))


def test_sort_rows_orders_by_each_choice_and_keeps_ties_in_bethesdas_order():
    def row(title, **kw):
        return {"title": title, "subscribes": 0, "downloads": 0, "likes": 0, "rating": None, "published": 0, "updated": 0, **kw}

    rows = [row("beta", subscribes=5, downloads=10, likes=3, rating=1, published=300, updated=100),
            row("Alpha", subscribes=9, downloads=10, likes=1, rating=2, published=100, updated=300),
            row("gamma", subscribes=7, downloads=40, likes=9, rating=2, published=200, updated=200),
            row("Delta", subscribes=7, downloads=None, likes=None, rating=None, published=None, updated=None)]
    order = lambda mode: [r["title"] for r in core.sort_rows(rows, mode)]  # noqa: E731
    assert order("match") == ["beta", "Alpha", "gamma", "Delta"]
    assert order("popular") == ["Alpha", "gamma", "Delta", "beta"]  # the 7s keep their order
    assert order("downloads") == ["gamma", "beta", "Alpha", "Delta"]
    assert order("likes") == ["gamma", "beta", "Alpha", "Delta"]
    assert order("rating") == ["gamma", "Alpha", "beta", "Delta"]  # rating, then likes; no rating last
    assert order("newest") == ["beta", "gamma", "Alpha", "Delta"]
    assert order("updated") == ["Alpha", "gamma", "beta", "Delta"]
    assert order("title") == ["Alpha", "beta", "Delta", "gamma"]  # case-insensitive
    assert [r["title"] for r in rows] == ["beta", "Alpha", "gamma", "Delta"]  # the input is untouched
    with pytest.raises(core.CreationsError):
        core.sort_rows(rows, "price")


def test_the_client_key_is_fetched_once_even_when_jobs_ask_at_the_same_time(net):
    import threading
    results = []
    threads = [threading.Thread(target=lambda: results.append(core.client_key())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8 and sum(u == core.CORE_URL for u, _ in net.calls) == 1


# ---------------------------------------------------------------- store prices (display only)

def _listing(store, amount, original=None, currency="VIRTUAL"):
    return {"platform": store, "prices": [{"currency_type": currency, "amount": amount,
                                           "original_amount": amount if original is None else original}]}


def test_the_store_price_is_the_first_pc_stores_credits_price():
    item = {"catalog_info": [_listing("XBOXLIVE", 900), _listing("EPIC", 500), _listing("STEAM", 400)]}
    assert core.store_price(item) == {"credits": 400, "was": None, "store": "STEAM"}  # Steam first; consoles never count
    assert core.store_price({"catalog_info": [_listing("EPIC", 500)]})["store"] == "EPIC"
    assert core.store_price({"catalog_info": [_listing("STEAM", 300, original=400)]})["was"] == 400
    assert core.store_price({"catalog_info": [_listing("STEAM", 0)]})["credits"] == 0


@pytest.mark.parametrize("catalog", [None, "", [], [_listing("XBOXLIVE", 400)], [_listing("STEAM", 400, currency="REAL")],
                                     [_listing("STEAM", -5)], [_listing("STEAM", True)], [_listing("STEAM", "lots")],
                                     [{"platform": "STEAM", "prices": "400"}], ["STEAM"], [_listing("STEAM", 10 ** 9)]])
def test_odd_or_missing_store_listings_give_no_price(catalog):
    assert core.store_price({"catalog_info": catalog}) is None


def test_a_store_listing_still_means_not_free_whatever_its_price():
    summary = core._summary("fallout4", api_item(catalog=[_listing("STEAM", 0)]))
    assert summary["free_status"] == "paid" and not summary["downloadable"] and summary["price"]["credits"] == 0



# ---------------------------------------------------------------- plain link errors and the game a link names

def test_link_errors_are_plain_sentences_without_internal_keys_or_hosts():
    def message(game, ref):
        with pytest.raises(core.CreationsError) as exc:
            core.resolve(game, ref)
        assert exc.value.status == "INVALID_ARGUMENT"
        return str(exc.value)
    assert message("fallout4", "https://evil.example.com/details/" + UUID) == "Only creations.bethesda.net links are understood."
    assert message("fallout4", "https://creations.bethesda.net/en/fallout4/details/nothing") == "That link doesn't contain a Creation id."
    assert message("fallout4", f"https://creations.bethesda.net/en/skyrim/details/{UUID}") == "That link is for Skyrim Special Edition."
    assert message("skyrimspecialedition", f"https://creations.bethesda.net/en/fallout4/details/{UUID}") == "That link is for Fallout 4."
    for text in ("skyrimspecialedition", "'fallout4'", "evil.example.com"):
        assert text not in message("fallout4", f"https://creations.bethesda.net/en/skyrim/details/{UUID}")


@pytest.mark.parametrize("ref,game", [
    (f"https://creations.bethesda.net/en/skyrim/details/{UUID}/Mod", "skyrimspecialedition"),
    (f"creations.bethesda.net/en/fallout4/details/{UUID}", "fallout4"),
    (f"https://creations.bethesda.net/en/SkyrimSpecialEdition/details/{UUID}", "skyrimspecialedition"),
    (f"https://creations.bethesda.net/en/details/{UUID}", None),  # names no game
    (f"https://evil.example.com/en/skyrim/details/{UUID}", None),  # not Bethesda's host
    (UUID, None), ("743724", None), ("skyrim mod", None), ("", None), (None, None), ("https://[bad", None)])
def test_the_game_a_link_names(ref, game):
    assert core.link_game(ref) == game


# ---------------------------------------------------------------- the refused file is named

def refusal(tmp_path, name):
    with pytest.raises(core.CreationsError) as exc:
        convert(tmp_path, btar([("data\\Fine.esp", PLUGIN), (name, b"x")]))
    assert exc.value.status == "UNSAFE_CKM"
    return str(exc.value)


def test_a_refused_member_is_named_in_the_error(tmp_path):
    assert refusal(tmp_path, "data\\foo.exe") == "Data/foo.exe isn't a type this plugin accepts"
    assert refusal(tmp_path, "Root\\loader.esp") == "Root/loader.esp isn't inside the Data folder"
    assert refusal(tmp_path, "data\\..\\evil.esp") == "data/../evil.esp has an unsafe file path"
    assert refusal(tmp_path, "data\\CON.esp") == "data/CON.esp uses a Windows device name"


def test_a_hostile_member_name_is_shown_cleaned_and_short(tmp_path):
    text = refusal(tmp_path, "data\\" + "x" * 300 + ".exe")
    assert len(text) < 120 and "…" in text and text.endswith(".exe isn't a type this plugin accepts")
    hostile = refusal(tmp_path, "data\\" + "x" * 300 + "‮\n\x07.exe")  # control characters make the path itself unsafe
    assert len(hostile) < 120 and hostile.endswith(".exe has an unsafe file path")
    for bad in ("\n", "\x07", "‮", ""):
        assert bad not in hostile
    odd = refusal(tmp_path, "data\\a‮<b>.esp")
    assert "‮" not in odd and "unsafe file path" in odd
    assert core.member_label("") == "(unnamed)" and core.member_label(None) == "(unnamed)"
    assert core.member_label("a\\b\\c.esp") == "a/b/c.esp"
    assert len(core.member_label("q" * 500, limit=40)) == 40


def test_more_inert_game_data_types_are_allowed_and_nothing_else_was_added(tmp_path):
    extra = [".hkx", ".tri", ".egm", ".egt", ".bik", ".seq", ".lod", ".btr", ".bto", ".btt", ".ssf", ".cdf", ".gid"]
    out = convert(tmp_path, btar([("data\\Fine.esp", PLUGIN)] + [(f"data\\Stuff\\a{ext.upper()}", b"d") for ext in extra]))
    assert len(out["files"]) == 1 + len(extra)
    assert set(extra) <= core.ALLOWED_SUFFIXES
    for refused in (".json", ".xml", ".dll", ".exe", ".bat", ".ps1", ".lua", ".js", ".cmd", ".vbs", ".py", ".jar", ".lnk"):
        assert refused not in core.ALLOWED_SUFFIXES


# ---------------------------------------------------------------- the Creations the game itself lists (1.1.0)

FO4_A, FO4_B = "3eca22b5-a6af-4356-a150-28fc016bbd50", "7A1B2C3D-0000-4000-8000-0123456789AB"


def entry(title="A Creation", version="1.0"):
    return {"Files": ["Fine.esp"], "FilesSize": 123, "Timestamp": 1700000000, "Title": title, "Version": version}


def write_catalog(base: Path, folder: str, body, *, text: str | None = None) -> Path:
    path = base / folder / "ContentCatalog.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else json.dumps(body), encoding="utf-8")
    return path


def trailer():
    return {"ContentCatalog": {"Description": "Content catalog", "Version": "1.1"}}


def test_a_fallout4_list_is_read_by_uuid(tmp_path):
    write_catalog(tmp_path, "Fallout4", {f"CSV2_{FO4_A}": entry("Vegas Style Dialogue Checks"), f"CSV2_{FO4_B}": entry(), **trailer()})
    got = core.game_catalog("fallout4", tmp_path)
    assert got == {"ids": set(), "uuids": {FO4_A, FO4_B.lower()}, "count": 2}  # UUIDs are kept in lower case


def test_a_skyrim_list_is_read_by_number_and_odd_versions_are_ignored(tmp_path):
    write_catalog(tmp_path, "Skyrim Special Edition", {"CSV2_5615": entry("Fishing", "150669.== Version Number =="),
                                                       "CSV2_5617": entry("Rare Curios", "7.2"), **trailer()})
    got = core.game_catalog("skyrimspecialedition", tmp_path)
    assert got == {"ids": {5615, 5617}, "uuids": set(), "count": 2}
    assert core.game_catalog("skyrim", tmp_path) == got and core.game_catalog("SkyrimSE", tmp_path) == got
    assert core.game_catalog("fallout4", tmp_path) == {"ids": set(), "uuids": set(), "count": 0}  # the other game has no list


def test_the_gog_and_epic_lists_are_merged_with_steams(tmp_path):
    write_catalog(tmp_path, "Skyrim Special Edition", {"CSV2_1": entry(), "CSV2_2": entry()})
    write_catalog(tmp_path, "Skyrim Special Edition GOG", {"CSV2_2": entry(), "CSV2_3": entry()})
    write_catalog(tmp_path, "Skyrim Special Edition EPIC", {"CSV2_4": entry(), f"CSV2_{FO4_A}": entry()})
    got = core.game_catalog("skyrimspecialedition", tmp_path)
    assert got["ids"] == {1, 2, 3, 4} and got["uuids"] == {FO4_A} and got["count"] == 5  # a repeat counts once
    assert [p.parent.name for p in core.catalog_paths("skyrimspecialedition", tmp_path)] == [
        "Skyrim Special Edition", "Skyrim Special Edition GOG", "Skyrim Special Edition EPIC"]
    write_catalog(tmp_path, "Fallout4", {f"CSV2_{FO4_B}": entry()})
    assert core.game_catalog("fallout4", tmp_path)["count"] == 1  # Fallout 4 never reads the Skyrim folders


def test_other_keys_and_other_prefixes(tmp_path):
    body = {"CSV2_5615": entry(), "ABC12345_77": entry(), f"XY9_{FO4_A}": entry(),  # any [A-Z0-9]{1,8} prefix
            "csv2_9": entry(), "CSV2_": entry(), "CSV2_1234567890": entry(), "CSV2_12a": entry(), "CSV2_-5": entry(),  # bad keys
            "TOOLONGPFX_5": entry(), "CSV2_5615x": entry(), "CSV2_" + FO4_A + "0": entry(), "_5": entry(), "5615": entry(),
            "CSV2_6": "not an object", "CSV2_7": [1, 2], "CSV2_8": None, **trailer()}
    assert core.game_catalog("fallout4", tmp_path)["count"] == 0  # nothing there yet
    write_catalog(tmp_path, "Fallout4", body)
    got = core.game_catalog("fallout4", tmp_path)
    assert got["ids"] == {5615, 77} and got["uuids"] == {FO4_A} and got["count"] == 3


def test_a_broken_file_falls_back_to_the_key_scan(tmp_path):
    text = ('{"CSV2_100": {"Files": ["a.esp"], "Title": "One"}, "CSV2_' + FO4_A + '": {"Title": "Two", "Version": "x"},\n'
            ' "CSV2_200": {"Title": "cut off here", "Files": [ ')  # truncated: not JSON
    write_catalog(tmp_path, "Fallout4", None, text=text)
    assert core.game_catalog("fallout4", tmp_path) == {"ids": {100, 200}, "uuids": {FO4_A}, "count": 3}
    write_catalog(tmp_path, "Fallout4", None, text="\x00\x01garbage � not json at all")
    assert core.game_catalog("fallout4", tmp_path)["count"] == 0
    write_catalog(tmp_path, "Fallout4", None, text="")
    assert core.game_catalog("fallout4", tmp_path)["count"] == 0
    for odd in ("[1, 2, 3]", "null", '"CSV2_5"', "42", '{"CSV2_5": 5}'):  # valid JSON that is not a list of Creations
        write_catalog(tmp_path, "Fallout4", None, text=odd)
        assert core.game_catalog("fallout4", tmp_path)["count"] == 0, odd
    write_catalog(tmp_path, "Fallout4", None, text="[" * 100000)  # deeper than the parser allows: still no error
    assert core.game_catalog("fallout4", tmp_path)["count"] == 0
    write_catalog(tmp_path, "Fallout4", None, text="﻿" + json.dumps({"CSV2_9": entry()}))  # a byte order mark is fine
    assert core.game_catalog("fallout4", tmp_path)["ids"] == {9}


def test_at_most_2000_entries_are_taken(tmp_path):
    write_catalog(tmp_path, "Skyrim Special Edition", {f"CSV2_{i}": entry() for i in range(1, 3001)})
    got = core.game_catalog("skyrimspecialedition", tmp_path)
    assert got["count"] == core.CATALOG_MAX_ENTRIES == 2000 and got["ids"] == set(range(1, 2001))
    text = "{" + ",".join(f'"CSV2_{i}": {{"Title": "x"}}' for i in range(1, 3001)) + ", broken"  # the key scan has the same limit
    write_catalog(tmp_path, "Skyrim Special Edition", None, text=text)
    assert core.game_catalog("skyrimspecialedition", tmp_path)["count"] == 2000


def test_only_the_first_5_mb_of_a_huge_file_is_read(tmp_path, monkeypatch):
    cap = core.CATALOG_MAX_BYTES
    assert cap == 5 * 1024 * 1024
    body = ('{"CSV2_10": {"Title": "early"}, "CSV2_1": {"Title": "' + "x" * (cap + 1000)
            + '"}, "CSV2_2": {"Title": "late"}, "CSV2_3": {"Title": "later"}}')
    path = write_catalog(tmp_path, "Skyrim Special Edition", None, text=body)
    assert path.stat().st_size > cap
    got = core.game_catalog("skyrimspecialedition", tmp_path)
    assert got["ids"] == {10, 1} and got["count"] == 2  # CSV2_2 and CSV2_3 lie beyond the cap
    sizes = []
    real_open = Path.open

    def spy(self, mode="r", *a, **k):
        fh = real_open(self, mode, *a, **k)
        if self == path:
            real_read = fh.read
            fh.read = lambda n=-1: sizes.append(n) or real_read(n)
        return fh

    monkeypatch.setattr(Path, "open", spy)
    core.game_catalog("skyrimspecialedition", tmp_path)
    assert sizes and all(0 <= n <= cap + 1 for n in sizes)  # never an unbounded read()


def test_a_missing_or_unreadable_list_is_an_empty_one(tmp_path, monkeypatch):
    empty = {"ids": set(), "uuids": set(), "count": 0}
    assert core.game_catalog("fallout4", tmp_path) == empty
    assert core.game_catalog("fallout4", tmp_path / "does-not-exist") == empty
    (tmp_path / "Fallout4" / "ContentCatalog.txt").mkdir(parents=True)  # a folder where the file should be
    assert core.game_catalog("fallout4", tmp_path) == empty
    monkeypatch.delenv("LOCALAPPDATA")
    assert core.game_catalog("fallout4") == empty and core.catalog_paths("fallout4") == []
    with pytest.raises(core.CreationsError):
        core.game_catalog("oblivion", tmp_path)


def test_the_default_base_is_local_app_data_and_the_files_are_only_read(no_real_local_app_data):
    path = write_catalog(no_real_local_app_data, "Fallout4", {"CSV2_42": entry()})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert core.game_catalog("fallout4")["ids"] == {42}  # %LOCALAPPDATA% (the temporary one the fixture set)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["ContentCatalog.txt"]
    assert Path(no_real_local_app_data).resolve() != (Path.home() / "AppData" / "Local").resolve()  # never the real folder


def test_catalog_refs_come_in_one_fixed_order():
    assert core.catalog_refs({"ids": {30, 4, 100}, "uuids": {"bb", "aa"}, "count": 5}) == [4, 30, 100, "aa", "bb"]
    assert core.catalog_refs({"ids": set(), "uuids": set()}) == []


def test_content_ids_are_validated_before_anything_is_sent(net):
    got = core._content_params("fallout4", content_ids=["5615", 5617, UUID.upper(), UUID])
    assert got["content_ids"] == f"5615,5617,{UUID},{UUID}"
    assert core._content_params("fallout4", content_ids=("1",))["content_ids"] == "1"
    assert "content_ids" not in core._content_params("fallout4", content_ids=None)
    for bad in ([], "5615", b"5615", ["5615,5617"], ["5615", ""], ["1234567890"], [-5], [True], [1.5], [None], ["12a"], ["²"],
                ["5615 "], [UUID + "0"], ["../x"], ["5615&size=1"], [f"{UUID}\n"], list(range(1, 102)), {"a": 1}, 5615):
        with pytest.raises(core.CreationsError) as exc:
            core._content_params("fallout4", content_ids=bad)
        assert exc.value.status == "INVALID_ARGUMENT", bad
    assert len(core._content_params("fallout4", content_ids=list(range(1, 101)))["content_ids"].split(",")) == 100
    with pytest.raises(core.CreationsError):
        core.search("fallout4", content_ids=["not an id"])
    assert net.calls == []


def owned_net(net, n, start=1):
    for i in range(start, start + n):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"Owned {i:04d}")


def test_lookup_ids_asks_for_at_most_100_at_a_time_and_at_most_three_times(net):
    owned_net(net, 250)
    progress = []
    got = core.lookup_ids("fallout4", list(range(1, 251)), progress=lambda d, t: progress.append((d, t)))
    assert len(got["rows"]) == 250 and got["total"] == 250 and got["capped"] is False
    calls = content_params(net)
    assert [len(q["content_ids"].split(",")) for q in calls] == [100, 100, 50]
    assert all(q["size"] == "100" and q["page"] == "1" and q["product"] == "fallout4" for q in calls)
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert {r["content_id"] for r in got["rows"]} == set(range(1, 251))
    secrets_absent(got)


def test_lookup_ids_mixes_numbers_and_uuids_and_repeats_nothing(net):
    owned_net(net, 3)
    got = core.lookup_ids("skyrimspecialedition", [1, "2", uid(3), uid(3).upper(), 1, "01"])
    assert sorted(r["content_id"] for r in got["rows"]) == [1, 2, 3] and got["total"] == 4  # "01" is another spelling
    (q,) = content_params(net)
    assert q["content_ids"] == f"1,2,{uid(3)},01" and q["product"] == "skyrim"


def test_lookup_ids_says_so_when_there_are_more_than_300(net):
    owned_net(net, 350)
    got = core.lookup_ids("fallout4", list(range(1, 351)))
    assert got["capped"] is True and got["total"] == 350 and len(got["rows"]) == 300
    assert [len(q["content_ids"].split(",")) for q in content_params(net)] == [100, 100, 100]
    assert core.OWNED_REQUEST_CAP == 300


def test_lookup_ids_with_nothing_to_look_up_makes_no_request(net):
    assert core.lookup_ids("fallout4", []) == {"rows": [], "total": 0, "capped": False}
    assert net.calls == []
    with pytest.raises(core.CreationsError) as exc:
        core.lookup_ids("fallout4", ["nonsense"])
    assert exc.value.status == "INVALID_ARGUMENT"
    with pytest.raises(core.CreationsError):
        core.lookup_ids("fallout4", "12345")
    assert net.calls == []


def test_lookup_ids_can_be_cancelled_between_requests_and_skips_what_bethesda_does_not_know(net):
    owned_net(net, 150)
    asked = []

    def cancel():
        asked.append(1)
        return len(asked) > 1

    with pytest.raises(core.CreationsError) as exc:
        core.lookup_ids("fallout4", list(range(1, 151)), cancel=cancel)
    assert exc.value.status == "CANCELLED" and len(content_params(net)) == 1
    net.calls.clear()
    got = core.lookup_ids("fallout4", [1, 2, 999999])  # 999999 is unknown to the catalogue
    assert [r["content_id"] for r in got["rows"]] == [1, 2] and got["total"] == 3
