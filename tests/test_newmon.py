import io
import json

import numpy as np
from PIL import Image

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompProject

ENTRY = """\
#if P_FAMILY_PIKACHU
    [SPECIES_PIKACHU] =
    {
        .baseHP        = 35,
        .baseAttack    = 55,
        .baseDefense   = 40,
        .baseSpeed     = 90,
        .baseSpAttack  = 50,
        .baseSpDefense = 50,
        .types = MON_TYPES(TYPE_ELECTRIC),
        .catchRate = 190,
    #if P_UPDATED_EXP_YIELDS >= GEN_5
        .expYield = 112,
    #else
        .expYield = 82,
    #endif
        .abilities = { ABILITY_STATIC, ABILITY_NONE, ABILITY_LIGHTNING_ROD },
        .speciesName = _("Pikachu"),
        .natDexNum = NATIONAL_DEX_PIKACHU,
        .categoryName = _("Mouse"),
        .height = 4,
        .weight = 60,
        .description = COMPOUND_STRING(
            "It keeps its tail raised to monitor\\n"
            "its surroundings."),
        .frontPic = gMonFrontPic_Pikachu,
        .backPic = gMonBackPic_Pikachu,
        .palette = gMonPalette_Pikachu,
        .shinyPalette = gMonShinyPalette_Pikachu,
        .iconSprite = gMonIcon_Pikachu,
        .iconPalIndex = 2,
        FOOTPRINT(Pikachu)
        .levelUpLearnset = sPikachuLevelUpLearnset,
        .teachableLearnset = sPikachuTeachableLearnset,
        .formSpeciesIdTable = sPikachuFormSpeciesIdTable,
        .evolutions = EVOLUTION({EVO_ITEM, ITEM_THUNDER_STONE, SPECIES_RAICHU}),
    },
#endif //P_FAMILY_PIKACHU
"""


def build(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    d = root / "src/data/pokemon"
    (d / "species_info/gen_1_families.h").write_text(
        "    [SPECIES_BULBASAUR] =\n    {\n        .speciesName = _(\"Bulbasaur\"),\n        .natDexNum = NATIONAL_DEX_BULBASAUR,\n"
        "        .height = 7,\n        .weight = 69,\n        .iconSprite = gMonIcon_Bulbasaur,\n        .iconPalIndex = 0,\n    },\n"
        + ENTRY)
    (root / "src/data/pokemon/species_info.h").write_text(
        "const struct SpeciesInfo gSpeciesInfo[] =\n{\n    #include \"species_info/gen_1_families.h\"\n\n"
        "    [SPECIES_EGG] = { .speciesName = _(\"Egg\") },\n};\n")
    (d / "level_up_learnsets").mkdir(parents=True, exist_ok=True)
    (d / "level_up_learnsets/gen_9.h").write_text(
        "#if P_FAMILY_PIKACHU\nstatic const struct LevelUpMove sPikachuLevelUpLearnset[] = {\n"
        "    LEVEL_UP_MOVE( 1, MOVE_THUNDER_SHOCK),\n    LEVEL_UP_END\n};\n#endif\n")
    (d / "all_learnables.json").write_text(json.dumps({"PIKACHU": ["MOVE_THUNDERBOLT"]}, indent=2))
    (root / "include/constants/species.h").write_text(
        "enum __attribute__((packed)) Species {\n    SPECIES_NONE = 0,\n    SPECIES_BULBASAUR = 1,\n"
        "    SPECIES_PIKACHU = 25,\n    SPECIES_RAICHU = 26,\n\n    SPECIES_CUSTOM_START = SPECIES_RAICHU,\n"
        "    // Add any custom species between here and SPECIES_CUSTOM_END\n    SPECIES_CUSTOM_END,\n\n"
        "    SPECIES_EGG = SPECIES_CUSTOM_END,\n    NUM_SPECIES = SPECIES_EGG,\n};\n")
    (root / "include/constants/pokedex.h").write_text(
        "enum NationalDexOrder\n{\n    NATIONAL_DEX_NONE,\n    NATIONAL_DEX_BULBASAUR,\n    NATIONAL_DEX_PIKACHU,\n};\n\n"
        "#if P_GEN_9_POKEMON == TRUE\n    #define NATIONAL_DEX_COUNT  NATIONAL_DEX_PIKACHU\n#else\n"
        "    #define NATIONAL_DEX_COUNT  NATIONAL_DEX_BULBASAUR\n#endif\n")
    (d / "pokedex_orders.h").write_text(
        "const u16 gPokedexOrder_Alphabetical[] =\n{\n    NATIONAL_DEX_BULBASAUR,\n    NATIONAL_DEX_PIKACHU,\n};\n\n"
        "const u16 gPokedexOrder_Weight[] =\n{\n    NATIONAL_DEX_PIKACHU,\n    NATIONAL_DEX_BULBASAUR,\n};\n\n"
        "const u16 gPokedexOrder_Height[] =\n{\n    NATIONAL_DEX_PIKACHU,\n    NATIONAL_DEX_BULBASAUR,\n};\n")
    cfg = root / "include/config/pokemon.h"
    cfg.write_text(cfg.read_text() + "#define P_FAMILY_PIKACHU TRUE\n#define P_UPDATED_EXP_YIELDS GEN_LATEST\n"
                   "#define P_GEN_9_POKEMON TRUE\n#define P_LEARNSET_HELPER_TEACHABLE TRUE\n#define P_LVL_UP_LEARNSETS GEN_LATEST\n")
    (root / "include/config/general.h").write_text("#define GEN_5 4\n#define GEN_9 8\n#define GEN_LATEST GEN_9\n")
    (root / "include/constants/pokemon.h").write_text(
        "enum Type {\n    TYPE_NONE,\n    TYPE_ELECTRIC,\n    TYPE_FAIRY,\n};\n"
        "enum Ability {\n    ABILITY_NONE,\n    ABILITY_STATIC,\n    ABILITY_LIGHTNING_ROD,\n};\n")
    (root / "include/constants/moves.h").write_text("enum Move {\n    MOVE_NONE,\n    MOVE_THUNDER_SHOCK,\n    MOVE_THUNDERBOLT,\n};\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "species"], cwd=root, check=True)
    return root


def test_new_pokemon(tmp_path):
    root = build(tmp_path)
    dp = DecompProject(root)
    nm = dp.newmon
    plan = nm.check("Sparkit", "pikachu")
    assert plan["names"]["const"] == "SPECIES_SPARKIT" and plan["template_symbol"] == "Pikachu"
    assert "other forms" in plan["warnings"][0]
    art = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    art.paste((240, 200, 30, 255), (8, 8, 32, 32))
    buf = io.BytesIO()
    art.save(buf, "PNG")
    r = nm.create("Sparkit", "SPECIES_PIKACHU", {"types": ["TYPE_ELECTRIC", "TYPE_FAIRY"], "baseSpeed": 99,
                                                 "category": "Spark", "weight": 30},
                  evolves_from={"species": "SPECIES_BULBASAUR", "param": 12}, images={"front": buf.getvalue()})
    assert r["species"] == "SPECIES_SPARKIT" and r["dex"] == "NATIONAL_DEX_SPARKIT"
    species_h = (root / "include/constants/species.h").read_text()
    assert "    SPECIES_SPARKIT,\n    SPECIES_CUSTOM_END," in species_h
    dex = (root / "include/constants/pokedex.h").read_text()
    assert "NATIONAL_DEX_PIKACHU,\n    NATIONAL_DEX_SPARKIT,\n};" in dex
    assert "#define NATIONAL_DEX_COUNT  NATIONAL_DEX_SPARKIT\n#else\n    #define NATIONAL_DEX_COUNT  NATIONAL_DEX_BULBASAUR" in dex
    custom = (root / "src/data/pokemon/species_info/custom_families.h").read_text()
    assert "[SPECIES_SPARKIT] =" in custom and "gMonFrontPic_Sparkit" in custom and "FOOTPRINT(Sparkit)" in custom
    assert "sSparkitLevelUpLearnset" in custom and "formSpeciesIdTable" not in custom and ".evolutions" not in custom
    assert "#else" not in custom and ".expYield = 112," in custom  # only what the config compiles
    assert '#include "species_info/custom_families.h"' in (root / "src/data/pokemon/species_info.h").read_text()
    gfx = (root / "src/data/graphics/pokemon.h").read_text()
    assert 'gMonFrontPic_Sparkit[] = INCGFX_U32("graphics/pokemon/sparkit/anim_front.png"' in gfx
    assert 'gMonFootprint_Sparkit[] = INCGFX_U8("graphics/pokemon/sparkit/footprint.png"' in gfx
    for f in ("anim_front.png", "back.png", "icon.png", "normal.pal", "shiny.pal", "footprint.png"):
        assert (root / "graphics/pokemon/sparkit" / f).exists(), f
    learn = (root / "src/data/pokemon/level_up_learnsets/gen_9.h").read_text()
    assert "sSparkitLevelUpLearnset[] = {\n    LEVEL_UP_MOVE( 1, MOVE_THUNDER_SHOCK)" in learn
    assert json.loads((root / "src/data/pokemon/all_learnables.json").read_text())["SPARKIT"] == ["MOVE_THUNDERBOLT"]
    orders = (root / "src/data/pokemon/pokedex_orders.h").read_text()
    assert "NATIONAL_DEX_PIKACHU,\n    NATIONAL_DEX_SPARKIT,\n};" in orders.split("gPokedexOrder_Weight")[0]  # alphabetical
    assert "gPokedexOrder_Weight[] =\n{\n    NATIONAL_DEX_SPARKIT,\n    NATIONAL_DEX_PIKACHU," in orders  # 3.0 kg < 6.0 kg
    dp2 = DecompProject(root)
    sp = dp2.gamedata.get_species("SPECIES_SPARKIT")
    assert sp["species_name"] == "Sparkit" and sp["types"] == ["TYPE_ELECTRIC", "TYPE_FAIRY"]
    assert sp["stats"]["baseSpeed"] == 99 and sp["stats"]["baseHP"] == 35 and sp["dex"]["category"] == "Spark"
    assert sp["front_pic"] == "graphics/pokemon/sparkit/anim_front.png"
    evo = dp2.gamedata.get_species("SPECIES_BULBASAUR")["evolutions"]
    assert evo == [{"method": "EVO_LEVEL", "param": "12", "target": "SPECIES_SPARKIT", "target_name": "Sparkit", "extra": []}]
    front = np.array(Image.open(root / "graphics/pokemon/sparkit/anim_front.png"))
    pikachu = np.array(Image.open(root / "graphics/pokemon/pikachu/anim_front.png"))
    assert front.shape == (128, 64) and not np.array_equal(front, pikachu)  # the picture went in
    assert front[0, 0] == 0 and front[32, 32] != 0  # see-through around it, drawn in the middle
    assert np.array_equal(np.array(Image.open(root / "graphics/pokemon/sparkit/icon.png")),
                          np.array(Image.open(root / "graphics/pokemon/pikachu/icon.png")))  # the rest is copied


def test_new_pokemon_cli_and_errors(tmp_path, capsys):
    root = build(tmp_path)
    assert main(["new-pokemon", "Volty", "--from", "pikachu", "--stats", "40,50,40,60,50,95", "-p", str(root)]) == 0
    assert "added Volty (SPECIES_VOLTY)" in capsys.readouterr().out
    assert main(["new-pokemon", "Volty", "--from", "pikachu", "-p", str(root)]) != 0  # already exists
    assert "already exists" in capsys.readouterr().err
    assert main(["new-pokemon", "Bolt", "--from", "volty", "-p", str(root)]) == 0  # a new one made from a new one
    custom = (root / "src/data/pokemon/species_info/custom_families.h").read_text()
    assert "[SPECIES_BOLT]" in custom and "gMonIcon_Bolt" in custom


def test_description_into_a_shared_text_symbol():
    from pkmn_asset_editor.gamedata import c_string_value
    assert c_string_value("gPichuPokedexText", "a\nb", default="COMPOUND_STRING").startswith("COMPOUND_STRING(\n")
    assert c_string_value('_("x")', "a", default="COMPOUND_STRING") == 'COMPOUND_STRING("a")'
    assert c_string_value('ITEM_NAME("Potion")', "Big") == 'ITEM_NAME("Big")'
