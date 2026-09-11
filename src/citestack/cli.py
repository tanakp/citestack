import argparse
import json
from pathlib import Path

from citestack.config import Settings


def main():
    parser = argparse.ArgumentParser(description="Build, inspect, and serve documentation RAG")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Download pinned Kubernetes English docs")
    fetch.add_argument("--output", type=Path, default=Path("data/corpus.jsonl"))
    fetch.add_argument("--limit", type=int)
    ingest = commands.add_parser("ingest", help="Build an atomic index snapshot")
    ingest.add_argument("--corpus", type=Path, default=Path("data/corpus.jsonl"))
    ask = commands.add_parser("ask", help="Answer one question")
    ask.add_argument("question")
    ask.add_argument("--mode", choices=["extractive", "ollama"])
    evaluate = commands.add_parser("eval", help="Compare four retrieval strategies")
    evaluate.add_argument("--dataset", type=Path, default=Path("evals/kubernetes.jsonl"))
    evaluate.add_argument("--output", type=Path, default=Path("data/evaluation.json"))
    evaluate.add_argument("--min-hit-rate", type=float, default=0.8)
    serve = commands.add_parser("serve", help="Serve API and Swagger documentation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--structured-only", action="store_true", help="No RAG models/index needed")
    extract = commands.add_parser("extract", help="Extract a typed support ticket using Ollama")
    extract.add_argument("text")
    commands.add_parser("inspect", help="Read index metadata without loading models")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "extract":
        from citestack.extraction import TicketExtractor

        result = TicketExtractor(settings).extract(args.text)
        print(result.model_dump_json(indent=2))
        if result.status != "success":
            raise SystemExit(2)
        return
    if args.command == "fetch":
        from citestack.ingestion import fetch_kubernetes

        print(json.dumps(fetch_kubernetes(args.output, limit=args.limit), indent=2))
        return
    if args.command == "serve":
        import uvicorn

        factory = "create_structured_app" if args.structured_only else "create_app"
        uvicorn.run(
            f"citestack.api:{factory}",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=settings.shutdown_timeout,
        )
        return
    if args.command == "inspect":
        import sqlite3

        with sqlite3.connect(settings.index_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            print(
                json.dumps(
                    json.loads(db.execute("SELECT value FROM metadata").fetchone()[0]), indent=2
                )
            )
        return
    from citestack.index import Retriever, build_index
    from citestack.models import NeuralModels

    models = NeuralModels(settings, reranker=args.command != "ingest")
    if args.command == "ingest":
        print(json.dumps(build_index(args.corpus, settings, models), indent=2))
        return
    retriever = Retriever(settings, models)
    try:
        if args.command == "ask":
            from citestack.answers import AnswerService

            if args.mode:
                settings.answer_mode = args.mode
            print(
                AnswerService(retriever, settings).answer(args.question).model_dump_json(indent=2)
            )
        elif args.command == "eval":
            from citestack.evaluation import evaluate

            result = evaluate(retriever, args.dataset)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            summary = {
                mode: {k: v for k, v in metrics.items() if k != "results"}
                for mode, metrics in result["modes"].items()
            }
            print(json.dumps(summary, indent=2))
            if result["modes"]["reranked"]["hit_rate_at_k"] < args.min_hit_rate:
                raise SystemExit(1)
    finally:
        retriever.close()


if __name__ == "__main__":
    main()
