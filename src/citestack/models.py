import numpy as np

from citestack.config import Settings


class NeuralModels:
    """Pinned CPU models. Downloads are cached by Hugging Face, never committed."""

    def __init__(self, settings: Settings, *, reranker: bool = True):
        from sentence_transformers import CrossEncoder, SentenceTransformer

        self.settings = settings
        self.encoder = SentenceTransformer(
            settings.embedding_model,
            revision=settings.embedding_revision,
            device=settings.device,
            trust_remote_code=False,
        )
        self.tokenizer = self.encoder.tokenizer
        self.cross_encoder = (
            CrossEncoder(
                settings.reranker_model,
                revision=settings.reranker_revision,
                device=settings.device,
                trust_remote_code=False,
                max_length=512,
            )
            if reranker
            else None
        )

    @property
    def fingerprint(self) -> str:
        return f"{self.settings.embedding_model}@{self.settings.embedding_revision}"

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        if query:
            texts = ["Represent this sentence for searching relevant passages: " + t for t in texts]
        return np.asarray(
            self.encoder.encode(
                texts, batch_size=64, normalize_embeddings=True, show_progress_bar=len(texts) > 100
            ),
            dtype=np.float32,
        )

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        if self.cross_encoder is None:
            raise RuntimeError("Reranker was not loaded")
        return self.cross_encoder.predict(
            [(question, text) for text in texts], batch_size=32, show_progress_bar=False
        ).tolist()
