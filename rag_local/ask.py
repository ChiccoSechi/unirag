"""Asks the RAG a question from the terminal, on the real Vectorize and D1 data (development only).

    uv run python -m rag_local.ask "Come si svolge l'esame di Cryptography?"
    uv run python -m rag_local.ask --chunks 8 --show "..."
    uv run python -m rag_local.ask --quoted "Data Mining vale 9 CFU." "E in che semestre?"

Question and answer are printed only here, on your terminal: it is a development tool.
"""

import argparse

from rag_local import rag


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("question")
    a.add_argument("--chunks", type=int, default=rag.DEFAULT_EXCERPTS, help="chunks to search")
    a.add_argument("--model", help="Workers AI model ID (default: CF_LLM_MODEL)")
    a.add_argument("--show", action="store_true", help="also print the excerpts passed")
    a.add_argument("--quoted", help='bot message answered with Telegram\'s "Reply"')
    arg = a.parse_args()

    r = rag.ask(arg.question, top_k=arg.chunks, model=arg.model, quoted=arg.quoted)
    if arg.show:
        for e in r.excerpts:
            print(f"--- [{e.number}] {e.score:.3f} {', '.join(e.chunks)} · {e.title}")
            print(e.text, end="\n\n")
        print("=" * 60)
    if arg.quoted:
        print(f"(rewritten question: {r.question})\n")
    print(r.answer)
    if r.truncated:
        print("\n(answer cut by the token limit)")
    if r.sources:
        print("\nSources:")
        for e in r.sources:
            print(f"- {e.title or e.page}: {e.url}")
    print(f"\n[{len(r.excerpts)} excerpts · {r.neurons:.1f} neurons · {r.seconds:.1f} s]")


if __name__ == "__main__":
    main()
