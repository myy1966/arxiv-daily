"""Shared fixtures for arxiv_daily.py tests."""
import json
import logging
import socket
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

# Add parent directory (scripts/) to path so we can import arxiv_daily
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import arxiv_daily  # noqa: E402

# Import fake server
from fake_llm_server import FakeLLMHandler  # noqa: E402


# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

SAMPLE_PAPER_CHUNK = '''\
<dt>
  <a name="item1">[1]</a>
  <a href="https://arxiv.org/abs/2604.06189" title="Abstract" id="2604.06189">
    arXiv:2604.06189
  </a>
</dt>
<dd>
  <div class="meta">
    <div class="list-title mathjax"><span class="descriptor">Title:</span>
      ATANT: A Test Framework for Narrative Truth
    </div>
    <div class="list-authors"><a href="/search/?query=Alice+Smith">Alice Smith</a>, <a href="/search/?query=Bob+Jones">Bob Jones</a></div>
    <div class="list-comments mathjax"><span class="descriptor">Comments:</span>
      Accepted at ACL 2026. Code at <a href="https://github.com/example/atant">this https URL</a>
    </div>
    <div class="list-subjects"><span class="descriptor">Subjects:</span>
      <span class="primary-subject">Artificial Intelligence (cs.AI)</span>; Machine Learning (cs.LG)
    </div>
    <p class="mathjax">
      This paper presents a novel framework for evaluating narrative truth in AI systems using multi-step reasoning chains and human feedback.
    </p>
  </div>
</dd>'''

SAMPLE_PAPER_CHUNK_2 = '''\
<dt>
  <a name="item2">[2]</a>
  <a href="https://arxiv.org/abs/2604.07001" title="Abstract" id="2604.07001">
    arXiv:2604.07001
  </a>
</dt>
<dd>
  <div class="meta">
    <div class="list-title mathjax"><span class="descriptor">Title:</span>
      On the Convergence of Diffusion Models
    </div>
    <div class="list-authors"><a href="/search/?query=Carol+Lee">Carol Lee</a></div>
    <div class="list-subjects"><span class="descriptor">Subjects:</span>
      <span class="primary-subject">Machine Learning (cs.LG)</span>; Artificial Intelligence (cs.AI)
    </div>
    <p class="mathjax">
      We study the convergence properties of score-based diffusion models and provide new theoretical bounds for the sampling process in high-dimensional spaces.
    </p>
  </div>
</dd>'''

SAMPLE_PAPER_CHUNK_CROSS = '''\
<dt>
  <a name="item3">[3]</a>
  <a href="https://arxiv.org/abs/2604.08123" title="Abstract" id="2604.08123">
    arXiv:2604.08123
  </a>
</dt>
<dd>
  <div class="meta">
    <div class="list-title mathjax"><span class="descriptor">Title:</span>
      Cross-Domain Transfer Learning for Robotics
    </div>
    <div class="list-authors"><a href="/search/?query=Dave+Wang">Dave Wang</a>, <a href="/search/?query=Eve+Chen">Eve Chen</a></div>
    <div class="list-subjects"><span class="descriptor">Subjects:</span>
      <span class="primary-subject">Robotics (cs.RO)</span>; Artificial Intelligence (cs.AI)
    </div>
    <p class="mathjax">
      This work proposes a cross-domain transfer learning approach for robotic manipulation tasks that significantly reduces the need for domain-specific training data.
    </p>
  </div>
</dd>'''

SAMPLE_PAPER_CHUNK_REPLACEMENT = '''\
<dt>
  <a name="item4">[4]</a>
  <a href="https://arxiv.org/abs/2603.01234" title="Abstract" id="2603.01234">
    arXiv:2603.01234
  </a>
</dt>
<dd>
  <div class="meta">
    <div class="list-title mathjax"><span class="descriptor">Title:</span>
      Revisiting Attention Mechanisms in Vision Transformers
    </div>
    <div class="list-authors"><a href="/search/?query=Frank+Zhang">Frank Zhang</a></div>
    <div class="list-comments mathjax"><span class="descriptor">Comments:</span>
      Updated with new experimental results
    </div>
    <div class="list-subjects"><span class="descriptor">Subjects:</span>
      <span class="primary-subject">Computer Vision (cs.CV)</span>; Artificial Intelligence (cs.AI)
    </div>
    <p class="mathjax">
      This paper revisits attention mechanisms in vision transformers and proposes an improved multi-scale attention module that achieves better accuracy-efficiency trade-offs on standard benchmarks.
    </p>
  </div>
</dd>'''


def _build_listing_html(date_line, sections_html):
    """Helper to build a minimal arXiv listing page."""
    return f"""\
<html>
<head><title>arXiv test</title></head>
<body>
<div id="content">
<h1>cs.AI listing</h1>
<p>{date_line}</p>
{sections_html}
</div>
</body>
</html>"""


SAMPLE_LISTING_HTML = _build_listing_html(
    "Showing new listings for Friday, 11 April 2025",
    f"""\
<h3>New submissions (showing 2 of 2 entries)</h3>
<dl>
{SAMPLE_PAPER_CHUNK}
{SAMPLE_PAPER_CHUNK_2}
</dl>
<h3>Cross submissions (showing 1 of 1 entries)</h3>
<dl>
{SAMPLE_PAPER_CHUNK_CROSS}
</dl>
<h3>Replacement submissions (showing 1 of 1 entries)</h3>
<dl>
{SAMPLE_PAPER_CHUNK_REPLACEMENT}
</dl>""",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_paper_chunk():
    return SAMPLE_PAPER_CHUNK


@pytest.fixture
def sample_listing_html():
    return SAMPLE_LISTING_HTML


@pytest.fixture
def sample_paper_dict():
    return {
        "id": "2604.06189",
        "title": "ATANT: A Test Framework for Narrative Truth",
        "authors": "Alice Smith, Bob Jones",
        "subjects": "cs.AI; cs.LG",
        "comments": "Accepted at ACL 2026. Code at this https URL",
        "abstract": "This paper presents a novel framework for evaluating narrative truth in AI systems using multi-step reasoning chains and human feedback.",
        "code_urls": ["https://github.com/example/atant"],
    }


@pytest.fixture
def sample_data():
    """Complete Phase 1 output dict."""
    return {
        "listing_date": "2025-04-11",
        "page_type": "new",
        "field": "cs.AI",
        "sections": [
            {
                "name": "New submissions",
                "count": 2,
                "papers": [
                    {
                        "id": "2604.06189",
                        "title": "ATANT: A Test Framework for Narrative Truth",
                        "authors": "Alice Smith, Bob Jones",
                        "subjects": "cs.AI; cs.LG",
                        "comments": "Accepted at ACL 2026. Code at this https URL",
                        "abstract": "This paper presents a novel framework for evaluating narrative truth in AI systems using multi-step reasoning chains and human feedback.",
                        "code_urls": ["https://github.com/example/atant"],
                    },
                    {
                        "id": "2604.07001",
                        "title": "On the Convergence of Diffusion Models",
                        "authors": "Carol Lee",
                        "subjects": "cs.LG; cs.AI",
                        "abstract": "We study the convergence properties of score-based diffusion models and provide new theoretical bounds for the sampling process in high-dimensional spaces.",
                    },
                ],
            },
            {
                "name": "Cross submissions",
                "count": 1,
                "papers": [
                    {
                        "id": "2604.08123",
                        "title": "Cross-Domain Transfer Learning for Robotics",
                        "authors": "Dave Wang, Eve Chen",
                        "subjects": "cs.RO; cs.AI",
                        "abstract": "This work proposes a cross-domain transfer learning approach for robotic manipulation tasks that significantly reduces the need for domain-specific training data.",
                    },
                ],
            },
            {
                "name": "Replacement submissions",
                "count": 1,
                "papers": [
                    {
                        "id": "2603.01234",
                        "title": "Revisiting Attention Mechanisms in Vision Transformers",
                        "authors": "Frank Zhang",
                        "subjects": "cs.CV; cs.AI",
                        "comments": "Updated with new experimental results",
                        "abstract": "This paper revisits attention mechanisms in vision transformers and proposes an improved multi-scale attention module that achieves better accuracy-efficiency trade-offs on standard benchmarks.",
                    },
                ],
            },
        ],
    }


@pytest.fixture
def sample_llm_result():
    """LLM response dict matching sample_data papers."""
    return {
        "papers": [
            {
                "id": "2604.06189",
                "summary": "这篇论文提出了一个用于评估人工智能系统中叙事真实性的新框架，利用多步推理链和人类反馈进行验证。",
                "tags": ["LLM Reasoning", "AI Evaluation"],
                "venue": "ACL 2026",
            },
            {
                "id": "2604.07001",
                "summary": "本文研究了基于分数的扩散模型的收敛性质，为高维空间中的采样过程提供了新的理论界限。",
                "tags": ["Diffusion Models"],
                "venue": None,
            },
            {
                "id": "2604.08123",
                "summary": "本研究提出了一种跨领域迁移学习方法，用于机器人操纵任务，显著减少了对领域特定训练数据的需求。",
                "tags": ["Transfer Learning", "Robotics"],
                "venue": None,
            },
            {
                "id": "2603.01234",
                "summary": "本文重新审视了视觉Transformer中的注意力机制，提出了一种改进的多尺度注意力模块，在标准基准上实现了更好的精度-效率权衡。",
                "tags": ["Vision Transformer", "Attention Mechanism"],
                "venue": None,
            },
        ],
        "new_tags": ["AI Evaluation", "Vision Transformer", "Attention Mechanism"],
    }


@pytest.fixture
def fake_llm_server():
    """Start fake LLM server on a random port, yield (api_url, handler_class)."""
    # Reset class-level state
    FakeLLMHandler.error_mode = "none"
    FakeLLMHandler.error_sequence = []
    FakeLLMHandler.error_rate = 0.0
    FakeLLMHandler.request_counter = 0

    # Find free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), FakeLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1", FakeLLMHandler
    server.shutdown()


@pytest.fixture
def logger(tmp_path):
    """Test logger writing to tmp_path."""
    log = logging.getLogger("test_arxiv_daily")
    log.setLevel(logging.DEBUG)
    handler = logging.FileHandler(tmp_path / "test.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    log.addHandler(handler)
    yield log
    log.removeHandler(handler)
    handler.close()


@pytest.fixture
def tags_file(tmp_path):
    """Create a temporary tags.json with initial tags."""
    tags_path = tmp_path / "tags.json"
    tags_data = {
        "updated_at": "2025-04-10T12:00:00+08:00",
        "tags": ["LLM Reasoning", "Diffusion Models", "Transfer Learning", "Robotics"],
    }
    tags_path.write_text(json.dumps(tags_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return tags_path


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Make retries fast for all tests."""
    monkeypatch.setattr(arxiv_daily, "RETRY_DELAYS", [0.01, 0.01, 0.01])
