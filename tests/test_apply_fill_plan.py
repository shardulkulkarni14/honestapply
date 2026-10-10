"""The apply prompt embeds the precomputed fast-path fill plan, and persists it."""

from __future__ import annotations

import json


def test_build_instructions_embeds_the_fill_plan(tmp_path, monkeypatch):
    import yaml

    from honestapply import config
    from honestapply.stages import apply as apply_stage

    # Isolate the config/data tree under tmp and seed a real profile + selectors.
    monkeypatch.setattr(config.PATHS, "root", tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "profile.json").write_text(
        json.dumps(
            {
                "legal_name": {"first": "Shardul", "last": "Kulkarni"},
                "email": "s@example.com",
                "linkedin_url": "https://linkedin.com/in/x",
            }
        )
    )
    (cfg / "ats_selectors.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": {
                    "first_name": 'input[name="first"]',
                    "last_name": 'input[name="last"]',
                    "email": 'input[type="email"]',
                    "linkedin_url": 'input[name="linkedin"]',
                    "submit": "button",
                }
            }
        )
    )

    instr, path = apply_stage._build_instructions(
        "https://boards.greenhouse.io/acme/jobs/1", 4242, "greenhouse", dry_run=True
    )

    # Section renders, placeholder is substituted, real values appear.
    assert "Fast-path fill plan" in instr
    assert "{fill_plan}" not in instr
    assert "Shardul" in instr and "s@example.com" in instr

    # The plan is persisted next to the instructions, for provenance.
    plan_file = path.parent / "fill_plan.json"
    assert plan_file.exists()
    data = json.loads(plan_file.read_text())
    keys = {a["field_key"] for a in data["actions"]}
    assert {"first_name", "last_name", "email", "linkedin_url"} <= keys
    assert all(a["field_key"] != "submit" for a in data["actions"])
