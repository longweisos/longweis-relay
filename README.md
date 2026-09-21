# Longweis Relay

**Keep premium AI for the decisions that need it.**

Longweis Relay is built for AI workflows that run longer than a single prompt. It helps organize working context and routes suitable parts of a workflow through a more efficient path, while preserving premium-model capacity for high-value reasoning and review.

## Measured compute ranges

In our current private-beta testing, Relay has reached:

- **Up to about 50% less premium-model compute** on suitable everyday work
- **Up to about 90% less total model compute** on suitable long-memory work

These are workload-dependent upper ranges, not guarantees. Actual results depend on the task, selected premium model, usable working history, and measured Relay participation. Short or high-risk tasks may stay mostly on the premium model. Image, video, and audio generation are not included in these optimization ranges.

## Who it is for

Relay is designed for people who use AI for ongoing work—not just one-off questions:

- Developers and operators working through multi-step tasks
- Teams with recurring, context-heavy workflows
- People who want to keep their existing AI workflow while using compute more deliberately

## How to access Relay

Longweis Relay is currently available as a macOS private beta.

Use the **General API** to connect Relay with compatible AI tooling and runtimes. Access is provisioned through beta enrollment.

For setup details, see the [Getting Started guide](docs/GETTING_STARTED.md).

## Compatibility status

We list a runtime as supported only after exercising it against Relay with real prompts, tools, files, multiple turns, or concurrent work. Protocol compatibility by itself is not counted as proof.

### Tested and supported

- **OpenClaw** — short and long tasks, multi-turn work, tool use, concurrency, and recovery paths
- **OpenCode 1.18.30** — long prompts, tool calls, four-way concurrency, and cross-turn continuity; parallel workers use isolated state directories
- **Aider 0.82.3** — basic calls, real file edits, cross-turn goal retention, and three-way concurrent work
- **AutoGen 0.7.5** — real function-tool execution and three-way concurrent tool turns

### Coverage expanding — 持续补充中

- **OpenHands 0.9.8** — downloaded for testing, but startup is blocked by an unavailable historical E2B dependency; Relay compatibility is not yet judged
- **Image, video, and audio generation** — not optimized by Relay in the current release
- **Windows and Linux desktop clients** — not included in the current Apple-silicon Mac private beta
- **More OpenAI-compatible runtimes** — added to the supported list only after repeatable compatibility tests

Compatibility reflects the tested versions and scenarios above; it is not a guarantee for every release, plugin, provider, model, or workload.

## Private beta

Request access at **[relay.longweisos.com](https://relay.longweisos.com/)**.

During the beta, we are focused on reliability across real long-running workflows, clear usage visibility, and practical compatibility.

## About this repository

This repository is the public home for Longweis Relay documentation, release notes, compatibility information, and product updates. It is not a promise that every implementation component is open source.

## Support

For beta access or product support: [longweis.app@gmail.com](mailto:longweis.app@gmail.com)

---

A Longweis product.
