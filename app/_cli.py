"""Load explicit startup configuration before importing the MCP server."""
import argparse
from pathlib import Path


def configure(argv=None):
    parser = argparse.ArgumentParser(prog='1c-testpilot', description='1C Testpilot MCP server')
    parser.add_argument('--env-file', metavar='PATH',
                        help='Load a UTF-8 .env file; existing environment variables take precedence.')
    args = parser.parse_args(argv)
    if args.env_file is not None:
        try:
            with Path(args.env_file).open(encoding='utf-8-sig') as stream:
                from dotenv import load_dotenv
                load_dotenv(stream=stream, override=False)
        except OSError as exc:
            parser.error(f'Cannot read env file {args.env_file!r}: {exc.strerror or type(exc).__name__}')
        except UnicodeError:
            parser.error(f'Cannot read env file {args.env_file!r}: expected UTF-8 text.')


def main():
    configure()
    from server import main as run_server
    run_server()


if __name__ == '__main__':
    main()
