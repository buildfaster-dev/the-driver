from the_driver.ingester import _detect_language, _is_test_file, _is_binary, _should_skip


class TestDetectLanguage:
    def test_python(self):
        assert _detect_language("app.py") == "Python"

    def test_javascript(self):
        assert _detect_language("index.js") == "JavaScript"

    def test_typescript(self):
        assert _detect_language("app.ts") == "TypeScript"

    def test_unknown(self):
        assert _detect_language("Makefile") == "Other"

    def test_nested_path(self):
        assert _detect_language("src/components/Button.tsx") == "React TSX"


class TestIsTestFile:
    def test_python_test(self):
        assert _is_test_file("test_app.py") is True

    def test_js_test(self):
        assert _is_test_file("app.test.js") is True

    def test_spec_file(self):
        assert _is_test_file("app.spec.ts") is True

    def test_tests_dir(self):
        assert _is_test_file("tests/test_something.py") is True

    def test_not_test(self):
        assert _is_test_file("app.py") is False

    def test_jest_dir(self):
        assert _is_test_file("__tests__/Button.test.tsx") is True


class TestIsBinary:
    def test_image(self):
        assert _is_binary("photo.png") is True

    def test_python(self):
        assert _is_binary("app.py") is False

    def test_pdf(self):
        assert _is_binary("doc.pdf") is True

    def test_pyc(self):
        assert _is_binary("module.pyc") is True


class TestShouldSkip:
    def test_node_modules(self):
        assert _should_skip("node_modules/express/index.js") is True

    def test_git(self):
        assert _should_skip(".git/config") is True

    def test_venv(self):
        assert _should_skip(".venv/lib/site.py") is True

    def test_normal_path(self):
        assert _should_skip("src/app.py") is False
