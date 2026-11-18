"""PyInstaller entry point; imports remain reusable by the CLI and tests."""
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from agentradar.desktop import main
    raise SystemExit(main())
