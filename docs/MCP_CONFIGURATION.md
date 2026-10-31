# MCP Server Configuration

## Claude Desktop

Add to your Claude Desktop configuration file (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "matricula": {
      "command": "/path/to/matricula-toolkit/.venv/bin/python",
      "args": ["-m", "matricula_toolkit.mcp_server"],
      "env": {
        "MATRICULA_BASE_URL": "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/",
        "MATRICULA_BOOK": "01519",
        "MATRICULA_OUT_DIR": "/path/to/downloads",
        "MATRICULA_WAIT_SECONDS": "5",
        "MATRICULA_HEADLESS": "true"
      }
    }
  }
}
```

## VS Code / GitHub Copilot

Add to your MCP settings:

```json
{
  "servers": {
    "matricula": {
      "command": "python",
      "args": ["-m", "matricula_toolkit.mcp_server"],
      "env": {
        "MATRICULA_BASE_URL": "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/",
        "MATRICULA_BOOK": "01519",
        "MATRICULA_OUT_DIR": "./downloads"
      }
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `inspect_book` | Verify a book is accessible and get sample page info |
| `inspect_page` | Download and inspect a single page |
| `download_page` | Download a single page |
| `download_range` | Download a range of pages |
| `verify_image` | Verify a downloaded image is valid JPEG |
| `diagnose_error` | Diagnose a page download error |
| `prepare_ocr` | List images prepared for OCR |

## Example MCP Call

```json
{
  "method": "tools/call",
  "params": {
    "name": "download_range",
    "arguments": {
      "start": 104,
      "end": 200,
      "base_url": "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/",
      "book": "01519"
    }
  }
}
```