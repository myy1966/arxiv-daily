# arXiv Daily

[![Build and Deploy](https://github.com/myy1966/arxiv-daily/actions/workflows/pages-deploy.yml/badge.svg)](https://github.com/myy1966/arxiv-daily/actions/workflows/pages-deploy.yml)

A daily digest of interesting arXiv papers, built with [Jekyll](https://jekyllrb.com/) and the [Chirpy](https://github.com/cotes2020/jekyll-theme-chirpy/) theme.

🌐 **Live Site**: [https://myy1966.github.io/arxiv-daily](https://myy1966.github.io/arxiv-daily)

## Local Development

### Prerequisites

- [Ruby](https://www.ruby-lang.org/) >= 3.1
- [Bundler](https://bundler.io/)

### Setup

```bash
# Clone the repository
git clone https://github.com/myy1966/arxiv-daily.git
cd arxiv-daily

# Install dependencies
bundle install

# Start local server
bundle exec jekyll serve
```

The site will be available at `http://127.0.0.1:4000/arxiv-daily/`.

## Creating Posts

Create a new file in `_posts/` with the naming convention `YYYY-MM-DD-title.md`:

```markdown
---
title: "Your Post Title"
date: YYYY-MM-DD HH:MM:SS +0800
categories: [Category1, Category2]
tags: [tag1, tag2]
---

Your content here...
```

## Deployment

The site is automatically built and deployed to GitHub Pages via GitHub Actions on every push to the `main` branch. Ensure that **Settings > Pages > Source** is set to **GitHub Actions**.

## License

This project is published under [MIT License](LICENSE).
