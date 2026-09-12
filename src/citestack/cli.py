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
    backup = commands.add_parser("backup", help="Validate and copy an immutable snapshot")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Validate and atomically restore a snapshot")
    restore.add_argument("source", type=Path)
    quality = commands.add_parser("quality", help="Run failure-preserving quality gates")
    quality.add_argument("--suite", choices=["retrieval", "structured"], default="retrieval")
    quality.add_argument("--dataset", type=Path)
    quality.add_argument("--policy", type=Path, default=Path("evals/quality-policy.json"))
    quality.add_argument("--output", type=Path, default=Path("data/quality.json"))
    quality.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "quality":
        from citestack.quality import (
            QualityPolicy,
            compare_baseline,
            journal,
            provenance,
            retrieval_quality,
            structured_quality,
            write_report,
        )

        runtime = provenance()
        progress = journal(args.output.with_suffix(".cases.jsonl"))
        policy = QualityPolicy.model_validate_json(args.policy.read_text())
        dataset = args.dataset or Path(f"evals/{args.suite}-quality.jsonl")
        if args.suite == "structured":
            import asyncio

            from citestack.extraction import TicketExtractor
            from citestack.providers import OllamaProvider

            provider = OllamaProvider(settings)
            if not asyncio.run(provider.ready()):
                raise RuntimeError(
                    "Quality evaluation requires the configured model to be available"
                )
            metadata = asyncio.run(provider.model_metadata())
            report = structured_quality(
                TicketExtractor(settings), dataset, policy, progress=progress
            )
            report["model"] = metadata
            report["generation_config"] = {
                "max_attempts": settings.structured_max_attempts,
                "temperature": 0,
                "context_limit": 8192,
                "output_limit": 1200,
            }
        else:
            from citestack.index import Retriever
            from citestack.models import NeuralModels

            retriever = Retriever(settings, NeuralModels(settings))
            try:
                report = retrieval_quality(retriever, dataset, policy, progress=progress)
            finally:
                retriever.close()
        report["runtime"] = runtime
        if args.baseline:
            compare_baseline(report, json.loads(args.baseline.read_text()))
        status = write_report(report, args.output)
        print(json.dumps({"passed": report["passed"], "metrics": report["metrics"]}, indent=2))
        raise SystemExit(status)
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
    if args.command in {"inspect", "backup", "restore"}:
        from citestack.snapshots import copy_snapshot, inspect_snapshot

        if args.command == "inspect":
            manifest = inspect_snapshot(settings.index_path)
        elif args.command == "backup":
            manifest = copy_snapshot(settings.index_path, args.destination)
        else:
            manifest = copy_snapshot(args.source, settings.index_path, restore=True)
        print(json.dumps(manifest, indent=2))
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
