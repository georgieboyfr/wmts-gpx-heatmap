import re
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parent.parent


def dockerfile_instructions():
    """Instructions du Dockerfile, lignes de continuation (backslash final) recollées."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\r\n", "\n")
    joined = re.sub(r"\\\n", " ", text)
    return [line.strip() for line in joined.split("\n") if line.strip() and not line.lstrip().startswith("#")]


def test_dockerfile_has_no_literal_escape_sequences():
    # un "\n" littéral casse l'analyse du Dockerfile ("can't find = in \n")
    for line in dockerfile_instructions():
        assert "\\n" not in line, line


def test_dockerfile_label_matches_package_version():
    labels = " ".join(line for line in dockerfile_instructions() if line.startswith("LABEL"))
    assert f'org.opencontainers.image.version="{__version__}"' in labels


def test_label_and_env_are_key_value_pairs():
    for line in dockerfile_instructions():
        keyword, _, rest = line.partition(" ")
        if keyword in ("LABEL", "ENV"):
            for pair in re.findall(r'\S+="[^"]*"|\S+', rest):
                assert "=" in pair, f"{keyword} : « {pair} » n'est pas de la forme clé=valeur"
