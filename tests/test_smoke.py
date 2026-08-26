from gitagent import __version__
from gitagent.application.cli import build_parser
from gitagent.prompts.library import get_prompt_library


def test_public_package_imports() -> None:
    assert __version__ == "0.1.0"


def test_cli_parser_builds() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.provider is None


def test_packaged_prompts_load_and_validate() -> None:
    library = get_prompt_library()
    library.validate()
    assert library.keys()
