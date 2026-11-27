import os
from pathlib import Path
import unittest

from gsp import skill_catalog


ROOT = Path(__file__).resolve().parents[1]


class SkillMethodLoadingTests(unittest.TestCase):
    def test_active_skills_index_conditionally_loaded_methods(self):
        manifest = skill_catalog.load_manifest(ROOT)
        for entry in skill_catalog.public_entries(manifest):
            with self.subTest(owner=entry.name):
                file = ROOT / entry.public_paths[0]
                text = file.read_text(encoding="utf-8")
                preamble = text.split("<!-- sp-preserved-body:begin -->", 1)[0]
                self.assertIn("methods.md", preamble)
                self.assertIn("触发", preamble)

    def test_method_index_lists_each_method_once_and_does_not_create_skills(self):
        index = (
            ROOT / "skills/gsp-workflow/references/methods.md"
        ).read_text(encoding="utf-8")
        manifest = skill_catalog.load_manifest(ROOT)
        for method in skill_catalog.stage_methods(manifest):
            with self.subTest(method=method.name):
                full = method.name
                self.assertEqual(index.count(f"`{full}`"), 1)
                relative = os.path.relpath(
                    Path(method.internal_method_path),
                    Path("skills/gsp-workflow/references"),
                ).replace("\\", "/")
                self.assertIn(f"({relative})", index)
                self.assertTrue((ROOT / method.internal_method_path).is_file())
                self.assertFalse(
                    (ROOT / "skills" / full / "SKILL.md").exists(),
                    f"{full} must remain a conditional method, not a discoverable skill",
                )

    def test_shared_contracts_are_linked_once_instead_of_pasted_into_every_skill(self):
        cross = ROOT / "skills/gsp-workflow/references/cross-session.md"
        stages = ROOT / "skills/gsp-workflow/references/stage-transitions.md"
        self.assertTrue(cross.is_file())
        self.assertTrue(stages.is_file())
        cross_text = cross.read_text(encoding="utf-8")
        stage_text = stages.read_text(encoding="utf-8")
        self.assertIn("TaskContract", cross_text)
        self.assertNotIn("`Tnn-prompt.md` 和 `Tnn-brief.md`", cross_text)
        self.assertIn("常规推进只调用一次 `advance`", stage_text)
        self.assertIn("`handoff` 仅用于需要主动预览", stage_text)
        self.assertNotIn("先运行 `handoff`", stage_text)
        manifest = skill_catalog.load_manifest(ROOT)
        all_skills = "\n".join(
            (ROOT / entry.public_paths[0]).read_text(
                encoding="utf-8"
            )
            for entry in skill_catalog.public_entries(manifest)
        )
        self.assertNotIn("<!-- cross-session-contract -->", all_skills)
        self.assertNotIn("<!-- stage-transition-contract -->", all_skills)

    def test_active_entries_do_not_advertise_retired_skill_names(self):
        manifest = skill_catalog.load_manifest(ROOT)
        retired = set(skill_catalog.names_for_class(manifest, "retired", "internal"))
        for entry in skill_catalog.public_entries(manifest):
            with self.subTest(owner=entry.name):
                text = (
                    ROOT / entry.public_paths[0]
                ).read_text(encoding="utf-8")
                advertised = {
                    name for name in retired
                    if f"`{name}`" in text or f"→ {name}" in text
                }
                self.assertEqual(advertised, set())

    def test_shared_handoff_uses_conditional_methods_instead_of_retired_chain(self):
        text = (ROOT / "skills/gsp-workflow/references/output-handoff.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("freeze/TDD/browse/learn", text)
        self.assertIn("条件方法完成后返回调用者", text)

    def test_convergence_contract_is_loaded_only_by_applicable_owners(self):
        reference = (
            ROOT / "skills/gsp-workflow/references/convergence.md"
        ).read_text(encoding="utf-8")
        for phrase in (
            "每个任务只有一个可验收结果",
            "跨模块、接口、数据所有权或并行写入边界",
            "有序且各自可验的切片",
            "45 分钟 checkpoint",
            "90 分钟没有 accepted",
            "由 Sol 裁决",
            "重试或换 agent 不重置",
            "未知进程/outcome 不自动接受或终止",
            "固定当前 candidate digest 和 contract digest",
            "当前契约违约、引入回归或",
            "独立真实缺陷登记后续归属",
            "可选改善退出本收敛循环",
            "不是目录冻结",
            "不新增模型调用、技能、审批或报表",
            "candidate_digest",
            "contract_digest",
            "reviewer_context",
            "verified-context",
            "host_observation",
            "worker_repair",
            "sol_takeover",
            "repair_count",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, reference)

        expected = {"workflow", "writing-plans", "execute", "review"}
        referenced = set()
        public_text = []
        manifest = skill_catalog.load_manifest(ROOT)
        for entry in skill_catalog.public_entries(manifest):
            text = (
                ROOT / entry.public_paths[0]
            ).read_text(encoding="utf-8")
            public_text.append(text)
            if "references/convergence.md" in text:
                referenced.add(skill_catalog.short_name(entry.name))
        self.assertEqual(referenced, expected)
        self.assertNotIn("<!-- convergence-contract -->", "\n".join(public_text))


if __name__ == "__main__":
    unittest.main()
