# Third-party material

Application code is MIT licensed. Downloaded documents and model weights keep
their own licenses; the application license does not relicense them.

- Kubernetes website documentation: Kubernetes documentation contributors,
  [CC BY 4.0](https://github.com/kubernetes/website/blob/17133089068629ec12ca15c1bdf36a60d2671a74/LICENSE).
  Ingestion removes frontmatter, template shortcodes, comments, image references,
  and link destinations, then splits the text into overlapping chunks.
  Each citation links to the original file at the exact Git revision.
  Downloaded documents and indexes are excluded from Git.
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5):
  embedding model; review its model card and license before redistribution.
- [cross-encoder/ms-marco-MiniLM-L-6-v2](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2):
  reranker; review its model card and license before redistribution.
- Ollama models are separately installed and subject to their own licenses.

An example answer in `docs/` may contain attributed Kubernetes documentation
excerpts under CC BY 4.0. These are transformed excerpts, not original project code.
