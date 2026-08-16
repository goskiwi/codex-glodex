from glodex.agent.contracts import CandidateAttribute
from glodex.memory.blacklist import VerifiedBlacklistRule
from glodex.memory.models import MemoryCandidate, UserMemoryCategory
from glodex.memory.normalization import normalize_blacklist_rule, persisted_memory_content
from tests.m1d.unit.test_agent_tools import _pool


def test_explicit_plastic_rejection_becomes_executable_rule() -> None:
    content = "以后都不要推荐塑料材质"
    candidate = MemoryCandidate(
        category=UserMemoryCategory.BLACKLIST,
        content=content,
        start=0,
        end=len(content),
    )

    assert persisted_memory_content(candidate) == "material:plastic"


def test_manual_blacklist_requires_structure_and_normalizes_known_values() -> None:
    assert normalize_blacklist_rule("material:塑料") == "material:plastic"
    assert normalize_blacklist_rule("platform:亚马逊") == "platform:amazon"

    try:
        normalize_blacklist_rule("不要塑料")
    except ValueError:
        pass
    else:
        raise AssertionError("unstructured manual blacklist must be rejected")


def test_normalized_material_rule_matches_trusted_chinese_candidate_fact() -> None:
    candidate = (
        _pool()
        .candidates[0]
        .model_copy(update={"attributes": (CandidateAttribute(name="材质", value="塑料"),)})
    )

    assert VerifiedBlacklistRule(field="material", value="plastic").matches(candidate)
