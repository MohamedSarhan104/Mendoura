#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def build_tailwind_css():
    """Compile Tailwind CSS before collectstatic runs.

    The configured Render build command calls `manage.py collectstatic`
    directly and never runs build.sh, so the Tailwind build step was being
    skipped entirely and the site shipped with no CSS. Hooking it in here
    guarantees it always runs, regardless of the exact build command.
    """
    import stat
    import subprocess
    import urllib.request
    from pathlib import Path

    base_dir = Path(__file__).resolve().parent
    bin_path = base_dir / '.tailwindcss-linux-x64'
    min_size = 1_000_000  # a real binary is several MB; smaller means a failed/partial download

    if not bin_path.is_file() or bin_path.stat().st_size < min_size:
        url = (
            'https://github.com/tailwindlabs/tailwindcss/releases/download/'
            'v3.4.19/tailwindcss-linux-x64'
        )
        print(f"Downloading Tailwind CSS CLI from {url}...")
        urllib.request.urlretrieve(url, bin_path)
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)

    input_css = base_dir / 'static_src' / 'input.css'
    output_css = base_dir / 'static' / 'css' / 'tailwind.css'
    output_css.parent.mkdir(parents=True, exist_ok=True)

    print("Compiling Tailwind CSS...")
    subprocess.run(
        [str(bin_path), '-i', str(input_css), '-o', str(output_css), '--minify'],
        check=True,
    )


def seed_reference_data():
    """Sync the Track and Plan catalogs after migrate.

    The configured Render build command runs `manage.py migrate` directly and
    never runs build.sh, so seed_tracks/seed_plans were never being invoked at
    all -- production has migrations applied but empty Track/Plan tables,
    which is why the tracks nav menu and pricing cards render with no
    content. Both commands are idempotent (update_or_create), so it's safe to
    run them on every migrate.
    """
    from django.core.management import call_command

    call_command('seed_tracks')
    call_command('seed_plans')


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'lms_backend.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    if 'collectstatic' in sys.argv:
        build_tailwind_css()

    execute_from_command_line(sys.argv)

    if 'migrate' in sys.argv:
        seed_reference_data()


if __name__ == '__main__':
    main()
