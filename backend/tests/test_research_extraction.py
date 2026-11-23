from forge.research.extraction import (
    extract_content,
    extract_html,
    extract_json_doc,
    extract_markdown,
)


def test_extract_html_strips_chrome_and_preserves_code():
    html_sample = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>FastAPI Tutorial - Security</title>
        <link rel="canonical" href="https://fastapi.tiangolo.com/tutorial/security/" />
        <script>console.log("analytics");</script>
        <style>.sidebar { color: red; }</style>
      </head>
      <body>
        <nav class="navbar"><a href="/">Home</a><a href="/docs">Docs</a></nav>
        <div class="cookie-banner">Accept cookies?</div>
        <main>
          <h1>Security First Steps</h1>
          <p>Let's check how to handle <strong>OAuth2</strong> with password and bearer tokens.</p>
          <h2>Code Example</h2>
          <pre><code>from fastapi import FastAPI, Depends

app = FastAPI()

@app.get("/users/me")
def read_current_user():
    return {"user": "current"}
</code></pre>
          <h3>Configuration Table</h3>
          <table>
            <tr><th>Setting</th><th>Default</th></tr>
            <tr><td>algorithm</td><td>HS256</td></tr>
          </table>
          <p>For more details see <a href="/advanced/">Advanced Guide</a>.</p>
        </main>
        <footer>Copyright 2026</footer>
      </body>
    </html>
    """
    res = extract_html(html_sample, base_url="https://fastapi.tiangolo.com/tutorial/security/")

    assert res.title == "FastAPI Tutorial - Security"
    assert res.canonical_url == "https://fastapi.tiangolo.com/tutorial/security/"
    assert "analytics" not in res.text
    assert "Accept cookies" not in res.text
    assert "Copyright 2026" not in res.text
    assert "Security First Steps" in res.headings
    assert "Code Example" in res.headings
    assert len(res.code_blocks) == 1
    assert "from fastapi import FastAPI" in res.code_blocks[0]
    assert "| Setting | Default |" in res.text
    assert "https://fastapi.tiangolo.com/advanced/" in res.links


def test_extract_markdown():
    md_sample = """# Pydantic V2 Migration

Welcome to the Pydantic v2 migration guide.

## Key Changes
- `regex` is replaced with `pattern`.
- `validator` is replaced with `field_validator`.

```python
from pydantic import BaseModel, field_validator

class User(BaseModel):
    name: str

    @field_validator('name')
    def validate_name(cls, v):
        return v.title()
```

Check the [Official Docs](https://docs.pydantic.dev/latest/) for full notes.
"""
    res = extract_markdown(md_sample, base_url="https://docs.pydantic.dev")
    assert res.title == "Pydantic V2 Migration"
    assert "Key Changes" in res.headings
    assert len(res.code_blocks) == 1
    assert "@field_validator" in res.code_blocks[0]
    assert "https://docs.pydantic.dev/latest/" in res.links


def test_extract_json_doc():
    json_sample = """{
        "title": "API Reference: Client",
        "description": "The HTTP client interface",
        "methods": [
            {"name": "get", "summary": "Issue an HTTP GET request"},
            {"name": "post", "summary": "Issue an HTTP POST request"}
        ]
    }"""
    res = extract_json_doc(json_sample)
    assert res.title == "API Reference: Client"
    assert "The HTTP client interface" in res.text
    assert "Issue an HTTP GET request" in res.text


def test_extract_content_dispatch():
    html_res = extract_content("<h1>Doc</h1><p>Content</p>", "text/html")
    assert html_res.title == "Doc"

    md_res = extract_content("# Markdown\nText", "text/markdown")
    assert md_res.title == "Markdown"

    plain_res = extract_content("Title\nSome raw text", "text/plain")
    assert plain_res.title == "Title"
    assert "Some raw text" in plain_res.text


def test_extract_html_malformed():
    malformed = "<html><head><title>Unclosed<body><h1>Heading<p>Text without closing"
    res = extract_html(malformed)
    assert res.title == "Unclosed"
    assert "Heading" in res.headings
    assert "Text without closing" in res.text
