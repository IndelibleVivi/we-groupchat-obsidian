import unittest
import tempfile
from pathlib import Path
from dataclasses import replace
from contextlib import closing

from ai.semantic_provider import EmbeddingProfile, EmbeddingResult, ProviderError, ProviderRoute
from ai.vector_index import LocalCosineIndex
from core.semantic.store import SemanticStore
from core.semantic.retrieval import HybridRetriever
from tests.test_semantic_engine import FakeDecision, FakeText, SemanticEngine, capture

from core.semantic.retrieval import SearchCoverage, SearchDocument, SearchResult, lexical_rank, lexical_tokens, reciprocal_rank_fusion, structural_candidates


class RetrievalFunctionsTests(unittest.TestCase):
    def test_chinese_details_and_identifiers_have_lexical_recall(self):
        docs = [SearchDocument("a", "source", "a", 1, "小李的旅行日记里提到了运营商的副卡选号步骤。"),
                SearchDocument("b", "item", "b", 1, "生活闲聊"),
                SearchDocument("c", "body", "c", 2, "方案 A17_B 支持双卡")]
        self.assertEqual(lexical_rank("副卡选号", docs)[0][0], "a")
        self.assertEqual(lexical_rank("A17_B", docs)[0][0], "c")
        self.assertIn("副卡", lexical_tokens("副卡选号"))

    def test_fusion_rewards_both_signals(self):
        ranking = reciprocal_rank_fusion([("a", 100), ("b", 1)], [("c", .9), ("b", .8)])
        self.assertEqual(ranking[0][0], "b")

    def test_directed_correction_bypasses_top_k_without_component_union(self):
        docs = [SearchDocument("a", "claim", "claim-a", 1, "匹配", item_id="item-a"),
                SearchDocument("b", "item", "item-b", 1, "完全不同的旧标题"),
                SearchDocument("c", "item", "item-c", 1, "近似向量但无依赖")]
        selected = structural_candidates(docs, correction_ids=("claim-a",), dependencies={"claim-a": ("item-b",)})
        self.assertEqual(selected, {"a", "b"})

    def test_replies_and_cases_bypass_display_limit(self):
        docs = [SearchDocument("a", "source", "new-reply", 1, "稍后附件", reply_to="original"),
                SearchDocument("b", "observation", "late-observation", 1, "同一用例", case_id="case-a")]
        self.assertEqual(structural_candidates(docs, source_ids=("original",), case_ids=("case-a",)), {"a", "b"})

    def test_exact_reply_source_traverses_affected_item_dependencies(self):
        docs = [SearchDocument("reply", "source", "reply-id", 1, "撤回先前说法", source_ids=("reply-source",), reply_to="old-source"),
                SearchDocument("old", "source", "old-source", 1, "旧证据", source_ids=("old-source",)),
                SearchDocument("affected", "item", "affected-item", 1, "没有词面相似性的依赖结果")]
        result = structural_candidates(docs, source_ids=("reply-source",),
                                       dependencies={"old-source": ("old-claim",), "old-claim": ("affected-item",)})
        self.assertEqual(result, {"reply", "old", "affected"})

    def test_search_absence_is_not_condition_false(self):
        coverage = SearchCoverage(1, 0, 0, 0, 0, 0, True)
        self.assertTrue(coverage.complete)
        self.assertEqual(SearchResult((), coverage, 0, ()).absence, "unknown")
        self.assertFalse(SearchCoverage(1, 10, 1, 1, 0, 0, True).complete)


class FakeEmbedding:
    profile = EmbeddingProfile("synthetic", "three-dimensional", 3)
    route = ProviderRoute("synthetic", "three-dimensional", "embedding")

    def __init__(self, *, mismatch=False):
        self.calls = []
        self.mismatch = mismatch

    def embed(self, texts, *, purpose, request_id, generation_id=""):
        self.calls.append((purpose, tuple(texts), request_id))
        vectors = tuple((0., 1., 0.) if "隐藏" in text or "synonym" in text else (1., 0., 0.) for text in texts)
        return EmbeddingResult(vectors, "wrong-profile" if self.mismatch else self.profile.profile_id, self.route)


class AsyncIndex(LocalCosineIndex):
    def __init__(self):
        super().__init__()
        self.ready = False
        self.calls = 0

    def upsert(self, records, **kwargs):
        self.calls += 1
        return replace(super().upsert(records, **kwargs), indexed=False)

    def confirm(self, receipt, **kwargs):
        return replace(receipt, indexed=self.ready)


class HybridIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SemanticStore(Path(self.temp.name).resolve() / "knowledge.sqlite", "retrieval-generation")

    def test_async_dense_tail_finds_unclassified_source_without_lexical_overlap(self):
        capture(self.store, "batch", ["普通聊天", "需要找到的隐藏细节"])
        embeddings, index = FakeEmbedding(), AsyncIndex()
        search = HybridRetriever(self.store, embeddings, index)
        result = search.index_pending()
        self.assertEqual(result["embedded"], 2)
        self.assertEqual(result["indexed"], 0)
        result = search.search("synonym", display_limit=1)
        self.assertEqual(result.hits[0].text, "需要找到的隐藏细节")
        self.assertEqual(result.hits[0].channels, ("dense",))
        self.assertEqual(result.hits[0].kind, "source")
        self.assertEqual(result.coverage.index_pending, 2)
        self.assertEqual(result.coverage.unread_sources, 2)
        self.assertFalse(result.coverage.complete)
        self.assertEqual(search.index_pending()["embedded"], 0)
        self.assertEqual(index.calls, 1)
        index.ready = True
        self.assertTrue(search.confirm_pending()[0]["indexed"])
        self.assertEqual(search.search("synonym").coverage.index_pending, 0)
        self.assertEqual(sum(call[0] == "query" for call in embeddings.calls), 1)

    def test_profile_mixing_is_rejected_before_persistence(self):
        capture(self.store, "batch", ["保留原文"])
        search = HybridRetriever(self.store, FakeEmbedding(mismatch=True), LocalCosineIndex())
        with self.assertRaisesRegex(ProviderError, "embedding_profile_or_count_mismatch"):
            search.index_pending()
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM embedding_records").fetchone()[0], 0)
        with self.assertRaisesRegex(ProviderError, "embedding_profile_mismatch"):
            HybridRetriever(self.store, FakeEmbedding(), LocalCosineIndex(),
                            profile=replace(FakeEmbedding.profile, query_template="Query: {text}"))

    def test_actual_local_index_rehydrates_from_confirmed_receipts(self):
        capture(self.store, "batch", ["需要找到的隐藏细节"])
        embeddings = FakeEmbedding()
        search = HybridRetriever(self.store, embeddings, LocalCosineIndex())
        self.assertEqual(search.index_pending()["indexed"], 1)
        reopened = HybridRetriever(self.store, embeddings, LocalCosineIndex())
        result = reopened.search("synonym")
        self.assertEqual(result.hits[0].text, "需要找到的隐藏细节")
        self.assertIn("dense", result.hits[0].channels)
        self.assertEqual(len([call for call in embeddings.calls if call[0] == "document"]), 1)

    def test_unknown_mutation_is_not_resent_but_local_evidence_remains(self):
        class UncertainIndex(LocalCosineIndex):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def upsert(self, records, **kwargs):
                self.calls += 1
                raise ProviderError("synthetic_network_unknown", outcome_unknown=True)
        capture(self.store, "batch", ["隐藏事实"])
        index = UncertainIndex()
        search = HybridRetriever(self.store, FakeEmbedding(), index)
        with self.assertRaisesRegex(ProviderError, "synthetic_network_unknown"):
            search.index_pending()
        self.assertEqual(search.index_pending()["indexed"], 0)
        self.assertEqual(index.calls, 1)
        self.assertEqual(search.search("synonym").hits[0].text, "隐藏事实")

    def test_silent_raw_observation_item_and_body_references_share_one_search(self):
        capture(self.store, "batch", ["花园隐藏计划"])
        SemanticEngine(self.store, FakeText(), FakeDecision()).process_batch("batch")
        search = HybridRetriever(self.store, FakeEmbedding(), LocalCosineIndex())
        search.index_pending()
        kinds = {hit.kind for hit in search.search("隐藏", display_limit=20).hits}
        self.assertTrue({"source", "observation", "item", "body", "change"} <= kinds)
        capture(self.store, "silent", ["未归类的隐藏故事"])
        result = search.search("未归类", display_limit=20)
        self.assertTrue(any(hit.kind == "source" and "未归类" in hit.text for hit in result.hits))
        self.assertGreater(result.coverage.embedding_pending, 0)

    def test_shared_index_pending_guard_blocks_another_generation(self):
        capture(self.store, "first", ["第一代隐藏证据"])
        second = SemanticStore(Path(self.temp.name).resolve() / "generation-b.sqlite", "generation-b")
        capture(second, "second", ["第二代隐藏证据"])
        shared = Path(self.temp.name).resolve() / "shared-index-locks"
        index = AsyncIndex()
        first_search = HybridRetriever(self.store, FakeEmbedding(), index, shared_lock_root=shared)
        second_search = HybridRetriever(second, FakeEmbedding(), index, shared_lock_root=shared)
        first_search.index_pending()
        self.assertEqual(index.calls, 1)
        second_search.index_pending()
        self.assertEqual(index.calls, 1)
        self.assertEqual(second_search.search("synonym").hits[0].text, "第二代隐藏证据")
        index.ready = True
        first_search.confirm_pending()
        second_search.index_pending()
        self.assertEqual(index.calls, 2)

    def test_typed_rerank_artifact_replays_and_invalid_score_stays_unknown(self):
        capture(self.store, "batch", ["隐藏事实"])
        decision = FakeDecision()
        search = HybridRetriever(self.store, FakeEmbedding(), LocalCosineIndex(), decision_provider=decision)
        search.index_pending()
        first = search.search("synonym")
        self.assertEqual(len(decision.calls), 1)
        self.assertEqual(search.search("synonym"), first)
        self.assertEqual(len(decision.calls), 1)
        class InvalidDecision(FakeDecision):
            route = ProviderRoute("synthetic", "invalid-decision", "decision")
            def decide(self, request, **kwargs):
                result = super().decide(request, **kwargs)
                return replace(result, answers={key: replace(answer, score=0) for key, answer in result.answers.items()})
        invalid = HybridRetriever(self.store, FakeEmbedding(), LocalCosineIndex(), decision_provider=InvalidDecision())
        result = invalid.search("synonym")
        self.assertIn("decision_score_probability_mismatch", result.coverage.unknown)
        self.assertTrue(result.hits)


if __name__ == "__main__":
    unittest.main()
