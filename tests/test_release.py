import hashlib
import json
import subprocess
import zipfile

import pytest

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.history import History
from pkmn_asset_editor.patch import apply_bps, apply_ups


def rom(seed: int, size: int = 2 << 20) -> bytes:
    b = bytearray((i * 7 + seed) & 0xFF for i in range(size))
    b[0xB2] = 0x96  # the GBA header's fixed byte
    return bytes(b)


@pytest.fixture()
def game(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    base = rom(1)
    hacked = bytearray(base)
    hacked[0x1000:0x1010] = b"MY HACK " * 2
    hacked += b"\xff" * 4096
    (root / "built.bin").write_bytes(bytes(hacked))
    (root / "Makefile").write_text("all:\n\t@echo built\nrelease:\n\tcp built.bin pokeemerald-release.gba\n")
    (root / "rom.sha1").write_text(hashlib.sha1(base).hexdigest() + "  pokeemerald.gba\n")
    (tmp_path / "emerald.gba").write_bytes(base)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=up@stream", "-c", "user.name=upstream", "commit", "-qm", "upstream work"],
                   cwd=root, check=True)
    return root, tmp_path / "emerald.gba", base, bytes(hacked)


def test_release(game):
    root, base_path, base, hacked = game
    dp = DecompProject(root)
    rel = dp.release
    assert any(c["level"] == "error" for c in rel.checks())  # no original ROM chosen yet
    with pytest.raises(DecompError, match="original ROM"):
        rel.build()
    rel.save_settings({"name": "Sunny Emerald", "version": "1.2", "author": "Me", "base_rom": str(base_path)})
    info = rel.base_info()
    assert info["known"] and info["name"] == "Pokémon Emerald Version (USA, Europe)"
    s = root / "data/maps/Town/scripts.inc"
    s.write_text(s.read_text() + "\n@ a change\n")
    History(dp).save("Added a sign to Town")
    assert rel.changelog() == ["Added a sign to Town"]  # not the upstream history
    r = rel.build(formats=("bps", "ups"))
    assert r["files"] == ["Sunny-Emerald-1-2.bps", "Sunny-Emerald-1-2.ups", "README.txt"]
    out = root / ".pae/releases/Sunny-Emerald-1-2"
    assert apply_bps(base, (out / "Sunny-Emerald-1-2.bps").read_bytes()) == hacked
    assert apply_ups(base, (out / "Sunny-Emerald-1-2.ups").read_bytes()) == hacked
    readme = (out / "README.txt").read_text()
    assert "Sunny Emerald 1.2" in readme and "by Me" in readme and "- Added a sign to Town" in readme
    assert hashlib.sha1(base).hexdigest() in readme and "Pokémon Emerald Version" in readme
    with zipfile.ZipFile(r["zip"]) as z:
        assert sorted(z.namelist()) == ["Sunny-Emerald-1-2/README.txt", "Sunny-Emerald-1-2/Sunny-Emerald-1-2.bps",
                                        "Sunny-Emerald-1-2/Sunny-Emerald-1-2.ups"]
    assert rel.changelog() == []  # nothing new since this release
    assert rel.settings()["releases"][0]["version"] == "1.2"


def test_release_cli_and_unknown_base(game, tmp_path, capsys):
    root, base_path, base, hacked = game
    other = tmp_path / "other.gba"
    other.write_bytes(rom(9))
    assert main(["release", "--name", "Test", "--version", "2", "--base", str(other), "-p", str(root)]) == 0
    out = capsys.readouterr().out
    assert "isn't the original ROM" in out and "Test-2.zip" in out
    from pkmn_asset_editor.mcp import Server
    srv = Server(DecompProject(root))
    r = json.loads(srv.call("make_release", {"version": "3", "base_rom": str(base_path)})[0]["text"])
    assert r["zip_name"] == "Test-3.zip"


def test_bps_finds_moved_data():
    import random
    from pkmn_asset_editor.patch import make_bps
    random.seed(4)
    blob = bytes(random.randrange(256) for _ in range(200_000))  # e.g. graphics the hack keeps, but elsewhere
    src = rom(1)[:1 << 20] + blob + rom(2)[:1 << 20]
    dst = rom(3)[:4096] + blob + rom(5)[:8192]
    patch = make_bps(src, dst)
    assert apply_bps(src, patch) == dst and len(patch) < 20_000
