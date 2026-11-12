"""Exercise the publication check with disposable repositories and fake credentials."""
import json
import subprocess

from scripts.check_secrets import main, scan


def commit(root):
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture'], check=True, capture_output=True)


def test_removed_credential_is_still_detected_in_history(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    fake = 'sk-or-v1-' + 'a' * 40
    path = tmp_path / 'settings.txt'
    path.write_text(fake)
    commit(tmp_path)
    path.write_text('removed')
    commit(tmp_path)
    result = scan(tmp_path)
    assert result['commits'] == 2
    assert any(f['rule'] == 'model_api_key' and ':' in f['location'] for f in result['findings'])
    assert fake not in json.dumps(result)


def test_staged_credential_is_detected_even_when_working_file_is_clean(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    fake = 'sk-or-v1-' + 'b' * 40
    path = tmp_path / 'settings.txt'
    path.write_text(fake)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'settings.txt'], check=True, capture_output=True)
    path.write_text('clean working copy')
    assert scan(tmp_path)['findings'] == [dict(location='index:settings.txt', rule='model_api_key')]


def test_configured_secret_and_state_are_detected_without_reading_ignored_files(tmp_path, monkeypatch, capsys):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    (tmp_path / '.gitignore').write_text('.env\n')
    fake = 'A9-configured-credential-canary'
    (tmp_path / '.env').write_text('APP_MODEL_API_KEY="' + fake + '"\n')
    (tmp_path / 'published.txt').write_text(fake)
    (tmp_path / 'state.sqlite3').write_bytes(b'SQLite format 3\x00synthetic')
    # A link to an ignored file is inspected as a link, never followed.
    (tmp_path / 'external-link').symlink_to(tmp_path / '.env')
    monkeypatch.chdir(tmp_path)
    assert main() == 1
    output = capsys.readouterr().out
    assert fake not in output
    result = json.loads(output)
    assert {f['rule'] for f in result['findings']} == {'configured_credential', 'private_path', 'database_content'}
    assert all(f['location'] not in ('.env', 'external-link') for f in result['findings'])
