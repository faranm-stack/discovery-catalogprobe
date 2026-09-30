"""Builds a tiny fake microsoft/discovery checkout for tests."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

GOOD_AGENT = """\
kind: prompt
name: good
model:
  id: '{{CHAT-MODEL}}'
instructions: |
  You operate the Good tool.

  ### Action: run
  **Parameters:**
    - `temperature` (number): Temperature in K

  ```python
  from good_utils import quick_setup, save_final_results
  quick_setup(input_dir='/input', output_dir='/output')
  save_final_results({"ok": 1}, output_dir='/output')
  ```
discoveryExtensions:
  tools:
    - toolId: '{{goodToolId}}'
"""

BAD_AGENT = """\
kind: prompt
name: bad
model:
  id: 'gpt-4.1'
instructions: |
  ### Action: run
    - `temprature` (number): typo'd parameter
  ### Action: simulate
    - `steps` (int): steps

  - plot_thing(data, output_path) -- makes a plot

  ```python
  from bad_utils import plot_thing, vanished_function
  plot_thing(data, output_path='/output/x.png')
  ```
  Also call `bad_utils.old_helper()` and `from missing_utils import x`.
  Use PreviewThingV1 for previews.
discoveryExtensions:
  tools:
    - toolId: '{{badToolId}}'
    - toolId: '{{anotherToolId}}'
"""

TOOL_YAML = """\
name: {name}
actions:
  - name: run
    input_schema:
      type: object
      properties:
        input_directory: {{type: string}}
        output_directory: {{type: string}}
        temperature: {{type: number}}
      required: [input_directory, output_directory]
"""

GOOD_UTILS = """\
def quick_setup(input_dir, output_dir):
    pass

def save_final_results(results, output_dir='/output', filename='final_results.json'):
    pass
"""

BAD_UTILS = """\
def plot_thing(data, output_file='plot.png', title=''):
    pass
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def fake_catalog(tmp_path: Path) -> Path:
    root = tmp_path / "discovery"
    for name, agent, utils_name, utils in (("good", GOOD_AGENT, "good_utils", GOOD_UTILS),
                                           ("bad", BAD_AGENT, "bad_utils", BAD_UTILS)):
        _write(root / "agents" / name / "agent.yaml", agent)
        _write(root / "agents" / name / "metadata.yaml",
               f"name: {name}\nassociated_tools:\n  - agents/{name}/tools/{name}\n")
        _write(root / "agents" / name / "tools" / name / "tool.yaml", TOOL_YAML.format(name=name))
        _write(root / "agents" / name / "tools" / name / "app" / f"{utils_name}.py", utils)
    kit = {"name": "demo-kit", "agentRefs": [{"ref": "agents/good", "role": "primary", "required": True},
                                             {"ref": "agents/ghost", "role": "supporting", "required": True}],
           "samplePrompts": [{"id": "sp-1", "prompt": "Run it at 300 K.",
                              "expectedOutput": "Pressure drop [Pa], inlet/outlet pressures."},
                             {"id": "sp-2", "prompt": "No expectation."}]}
    _write(root / "starter-kits" / "demo-kit" / "kit.json", json.dumps(kit))
    return root
