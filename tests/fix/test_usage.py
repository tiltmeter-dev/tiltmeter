from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fix.kit import commit_files, make_repo
from tiltmeter.fix.git import Git
from tiltmeter.fix.models import UnusedFile
from tiltmeter.fix.usage import STALE_DAYS, likely_dead, unused_files

OLD = "2024-01-15T12:00:00+00:00"

WEATHER_TS = """\
import axios from 'axios'

export async function getWeather(lat: number, lon: number, apiKey: string) {
  const res = await axios.get(
    `https://api.openweathermap.org/data/2.5/onecall?lat=${lat}&lon=${lon}&appid=${apiKey}`,
  )
  return res.data.hourly[0].weather[0].description
}
"""

# ospfranco/sol: nothing imports src/lib/weather.ts, but the emoji table says 'weather'.
SOL = {
    "src/lib/weather.ts": WEATHER_TS,
    "src/lib/emojis.ts": "export const emojis = [{ char: '⛅', tags: ['weather', 'cloud'] }]\n",
    "src/index.ts": "import { emojis } from './lib/emojis'\n\nconsole.log(emojis.length)\n",
    "src/widget.tsx": "export const Widget = () => <Text>The weather today</Text>\n",
    "README.md": "# Sol\n\n`src/lib/weather.ts` fetches the forecast.\n",
    "CHANGELOG.md": "- Moved the weather code to src/lib/weather.ts\n",
    "docs/widgets.md": "See [weather](../src/lib/weather.ts).\n",
    ".gitignore": "node_modules\nweather.ts.bak\n",
    "package.json": '{"name": "sol", "main": "index.js"}\n',
}


def repo_with(tmp_path: Path, files: dict[str, str], *, date: str | None = OLD) -> Git:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, _ = make_repo(tmp_path, files, date=date)
    return Git(repo)


def unused_paths(git: Git, *changed: str) -> list[str]:
    return [item.path for item in unused_files(git, list(changed))]


def test_the_sol_case_is_flagged_as_dead_and_old(tmp_path: Path) -> None:
    git = repo_with(tmp_path, SOL)
    [found] = unused_files(git, ["src/lib/weather.ts"])
    assert found.path == "src/lib/weather.ts"
    assert found.last_changed == OLD
    assert found.age_days > STALE_DAYS
    assert likely_dead(["src/lib/weather.ts"], [found])
    # A changelog entry next to it is still nothing but dead code.
    assert likely_dead(["src/lib/weather.ts", "CHANGELOG.md"], [found])
    assert not likely_dead(["src/lib/weather.ts", "src/index.ts"], [found])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("src/screens/home.tsx", "import { getWeather } from '../lib/weather'\n"),
        ("src/screens/home.tsx", "import { getWeather } from '@/lib/weather.js'\n"),
        ("src/lib/all.ts", "export * from './weather'\n"),
        ("src/lazy.ts", "const load = () => import('./lib/weather')\n"),
        ("src/old.js", "const weather = require('./lib/weather')\n"),
        ("src/lib/weather.test.ts", "jest.mock('./weather')\n"),
        ("src/baseurl.ts", "import { getWeather } from 'weather'\n"),
        ("package.json", '{"exports": {"./weather": "./dist/weather.js"}}\n'),
        ("Dockerfile", 'CMD ["npx", "tsx", "src/lib/weather.ts"]\n'),
        (".github/workflows/ci.yml", "      - run: node build/weather.js\n"),
        ("README.md", "Try it with `npx tsx src/lib/weather.ts`.\n"),
    ],
)
def test_import_forms_and_named_uses_count(tmp_path: Path, name: str, text: str) -> None:
    git = repo_with(tmp_path, {**SOL, name: text})
    assert unused_paths(git, "src/lib/weather.ts") == []


def test_python_import_forms(tmp_path: Path) -> None:
    module = "billing/weather.py"
    files = {
        module: "import httpx\n\n\ndef forecast() -> str:\n    return httpx.get('x').text\n",
        "billing/views.py": "weather = 'sunny'\nprint('The weather is nice')\n",
        "README.md": "billing.weather has the forecast.\n",
    }
    git = repo_with(tmp_path, files)
    assert unused_paths(git, module) == [module]  # a variable, a word and docs are no use
    uses = [
        ("billing/views.py", "from billing.weather import forecast\n"),
        ("billing/views.py", "from .weather import forecast\n"),
        ("billing/views.py", "from . import client, weather\n"),
        ("billing/views.py", "from billing import (\n    client,\n    weather,\n)\n"),
        ("billing/views.py", "import billing.weather as w\n"),
        ("billing/test_views.py", "patch('billing.weather.httpx')\n"),
        ("pyproject.toml", '[project.scripts]\nforecast = "billing.weather:main"\n'),
        ("Procfile", "web: uvicorn billing.weather:app\n"),
        ("Makefile", "forecast:\n\tpython -m billing.weather\n"),
        ("serverless.yml", "handler: billing.weather.handler\n"),
        ("README.md", "Run `python billing/weather.py` to print it.\n"),
    ]
    for index, (name, text) in enumerate(uses):
        git = repo_with(tmp_path / str(index), {**files, name: text})
        assert unused_paths(git, module) == [], (name, text)


def test_rust_modules_count_only_when_declared(tmp_path: Path) -> None:
    files = {
        "src/main.rs": "fn main() {}\n",
        "src/weather.rs": "pub fn forecast() -> String { String::new() }\n",
    }
    assert unused_paths(repo_with(tmp_path / "a", files), "src/weather.rs") == ["src/weather.rs"]
    files["src/main.rs"] = "mod weather;\n\nfn main() { weather::forecast(); }\n"
    assert unused_paths(repo_with(tmp_path / "b", files), "src/weather.rs") == []


@pytest.mark.parametrize(
    "name",
    [
        "src/index.ts",
        "src/main.tsx",
        "billing/__main__.py",
        "billing/__init__.py",
        "manage.py",
        "src/main.rs",
        "src/lib.rs",
        "vite.config.ts",
        "src/global.d.ts",
        "src/pages/weather.tsx",
        "app/forecast/page.tsx",
        "src/routes/+page.ts",
        "tests/test_weather.py",
        "src/weather.test.ts",
        "scripts/sync_weather.py",
        "plugins/weather.py",
        "assistant/handlers/weather.py",
        ".github/scripts/release.js",
    ],
)
def test_entry_points_and_listed_folders_count_as_used(tmp_path: Path, name: str) -> None:
    git = repo_with(tmp_path, {**SOL, name: "export const x = 1\n"})
    assert unused_paths(git, name) == []


def test_programs_and_files_in_a_series_count_as_used(tmp_path: Path) -> None:
    files = {
        "tools.js": "#!/usr/bin/env node\nconsole.log('hi')\n",
        "billing/report.py": "def main() -> None: ...\n\nif __name__ == '__main__':\n    main()\n",
        "voice/plugin_weather.py": "def run() -> None: ...\n",
        "voice/plugin_time.py": "def run() -> None: ...\n",
    }
    git = repo_with(tmp_path, files)
    assert unused_paths(git, *files) == []


def test_computed_imports_cover_the_folder_they_read(tmp_path: Path) -> None:
    files = {
        **SOL,
        "src/widgets/weather.ts": WEATHER_TS,
        "src/index.ts": "export const load = (name: string) => import(`./widgets/${name}`)\n",
    }
    git = repo_with(tmp_path / "js", files)
    assert unused_paths(git, "src/widgets/weather.ts", "src/lib/weather.ts") == [
        "src/lib/weather.ts"
    ]
    loader = (
        "import importlib\nimport os\n\n"
        "for name in os.listdir('skills'):\n    importlib.import_module(name)\n"
    )
    python = {"skills/weather.py": "x = 1\n", "assistant/loader.py": loader}
    assert unused_paths(repo_with(tmp_path / "py", python), "skills/weather.py") == []


def test_go_and_other_implicit_languages_are_never_flagged(tmp_path: Path) -> None:
    files = {
        **SOL,
        "weather/weather.go": 'package weather\n\nfunc Forecast() string { return "" }\n',
        "src/main/java/app/Weather.java": "class Weather {}\n",
        "app/models/weather.rb": "class Weather; end\n",
    }
    git = repo_with(tmp_path, files)
    changed = ["src/lib/weather.ts", *files.keys() - SOL.keys()]
    unused = unused_files(git, changed)
    assert [item.path for item in unused] == ["src/lib/weather.ts"]
    # Go may still need the fix: the pull request is not held back.
    assert not likely_dead(changed, unused)


ONE_CALL = """\
import Foundation

struct OCOneCall: Codable {
    let current: OCCurrent?
}

struct OCCurrent: Codable {
    let temp: Double?
}

extension OCCurrent {
    var celsius: Double { temp ?? 0 }
}
"""


def test_swift_files_count_by_the_names_they_declare(tmp_path: Path) -> None:
    # JohnCoates/Aerial: no other file uses OneCall.swift's types; the project lists the file.
    files = {
        "Aerial/Source/Models/API/OneCall.swift": ONE_CALL,
        "Aerial/Source/Models/API/Forecast.swift": "struct Forecast { let temp: Double }\n",
        "Aerial/Source/Views/WeatherLayer.swift": "let forecast = Forecast(temp: 1)\n",
        "Aerial.xcodeproj/project.pbxproj": "/* OneCall.swift in Sources */\n/* OCOneCall */\n",
        "Aerial/Source/Extensions/String+Trim.swift": "extension String {\n    func t() {}\n}\n",
    }
    git = repo_with(tmp_path, files)
    changed = [name for name in files if name.endswith(".swift")]
    assert unused_paths(git, *changed) == [
        "Aerial/Source/Models/API/OneCall.swift",
        "Aerial/Source/Views/WeatherLayer.swift",  # nothing uses its forecast either
    ]
    git = repo_with(
        tmp_path / "used", {**files, "Aerial/Main.swift": "_ = OCOneCall(current: nil)\n"}
    )
    assert unused_paths(git, "Aerial/Source/Models/API/OneCall.swift") == []


def test_a_recent_change_is_flagged_but_not_old(tmp_path: Path) -> None:
    git = repo_with(tmp_path, SOL)
    commit_files(git.root, {"src/lib/weather.ts": WEATHER_TS + "// touched\n"})
    [found] = unused_files(git, ["src/lib/weather.ts"])
    assert found.age_days <= 1
    assert not likely_dead(["src/lib/weather.ts"], [found])


def test_new_deleted_and_ignored_files_are_left_out(tmp_path: Path) -> None:
    git = repo_with(tmp_path, SOL)
    (git.root / "src/lib/new.ts").write_text("export const x = 1\n", encoding="utf-8")
    (git.root / "src/lib/weather.ts").unlink()
    git.stage_all()
    assert unused_paths(git, "src/lib/new.ts", "src/lib/weather.ts") == []
    git = repo_with(tmp_path / "ignored", SOL)
    assert unused_files(git, ["src/lib/weather.ts"], ignore=["src/lib"]) == []


def test_the_age_counts_from_the_review(tmp_path: Path) -> None:
    git = repo_with(tmp_path, SOL)
    now = datetime(2024, 7, 13, 12, tzinfo=UTC)
    assert unused_files(git, ["src/lib/weather.ts"], now=now) == [
        UnusedFile(path="src/lib/weather.ts", last_changed=OLD, age_days=180)
    ]
