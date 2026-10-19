# Product, brand and agent-workflow references

The implementation uses original project code and artwork. These public repositories informed the
workflow and editorial decisions; no third-party skill code is bundled or executed automatically.

| Source | Principle applied here | Result in this repository |
| --- | --- | --- |
| [cofoundy/brand-skills](https://github.com/cofoundy/brand-skills), especially [positioning](https://github.com/cofoundy/brand-skills/blob/main/skills/brand-positioning/SKILL.md) and [identity](https://github.com/cofoundy/brand-skills/blob/main/skills/brand-identity/SKILL.md) | Establish audience and promise before visual assets; preserve the same story in voice, palette and touchpoints. | `brand/brand.yaml`, brand guide, agent-team headline, routed-envelope mark and social asset. |
| [anthropics/skills frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design) | Ground design in the actual task, use hierarchy deliberately, and make empty/error states actionable. | A specialist handoff in the hero, operator-role feedback, focused key-management controls and shared UI tokens. |
| [erron-ai/marketing-skills](https://github.com/erron-ai/marketing-skills), especially [site architecture](https://github.com/erron-ai/marketing-skills/blob/main/skills/site-architecture/SKILL.md) | Organize routes around visitor intent and keep the product context reusable. | Product context, Spanish/English landing, use cases → deployment → connection journey, and explicit operator/agent docs. |
| [robertbstillwell/marketing-skills](https://github.com/robertbstillwell/marketing-skills) | Treat content, launch and social communication as reusable workflows. | English/Spanish launch drafts with factual claims and privacy requirements. |
| [obra/superpowers](https://github.com/obra/superpowers), including [porting guide](https://github.com/obra/superpowers/blob/main/docs/porting-to-a-new-harness.md) | A skill needs a host-supported bootstrap, and actions should map to real host interfaces. | `/workflow` delivers one master entry point; lifecycle hooks point to a private bank, while wake remains host-specific. |

The project does not claim that these sources certify its design or security. Capability claims,
adoption figures, uptime and savings must be supported by evidence before appearing in public copy.
