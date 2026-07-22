"""Mendoura AI Coach -- a thin wrapper around the Anthropic Messages API.

The one network call (send_message) is isolated here so tests can mock it,
same pattern as bunny.create_video / paymob's request helpers.
"""
import re

import anthropic
from django.conf import settings

MODEL = 'claude-opus-4-8'

SYSTEM_PROMPT = (
    "You are Mendoura AI Coach, a friendly, encouraging, and elite academic "
    "tutor. Your goal is to help students understand complex topics, write "
    "summaries, build study schedules, and explain concepts simply. Keep "
    "answers well-formatted with markdown and friendly tones."
)

# General-Purpose Sandbox AI Engine -- keeps the chat itself usable across
# any domain (demos, local dev, a deploy that hasn't got AI_API_KEY set yet)
# instead of locking the input and showing an admin-facing error. Clearly
# labeled as a preview in both the copy and the UI badge (see
# dashboard/ai_buddy.html) -- never presented as a real model response.
SANDBOX_TECH_GUIDE = """### \U0001f4bb Modern Software Engineering Best Practices (Sandbox Preview)

Here's a foundation the real AI Coach can tailor to your exact stack once it's connected:

1. **Write Readable Code First**
   - Clear naming beats clever naming
   - Small, single-purpose functions and files

2. **Version Control Discipline**
   - Small, focused commits with clear messages
   - One feature per branch, reviewed before merging

3. **Test as You Go**
   - Unit tests for logic, integration tests for full flows
   - A bug caught by a test today is an outage avoided in production

4. **Handle Errors Deliberately**
   - Fail loudly in development, gracefully in production
   - Never swallow exceptions silently -- log or re-raise

```python
def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
```

```javascript
function divide(a, b) {
  if (b === 0) throw new Error("Cannot divide by zero");
  return a / b;
}
```

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll tailor this to the language, framework, or bug you're actually working on.*"""

SANDBOX_BUSINESS_FRAMEWORK = """### \U0001f4c8 Elite Entrepreneurship Framework (Sandbox Preview)

| Stage | Focus | Key Question |
|---|---|---|
| Validate | Talk to real customers | Does this problem actually hurt? |
| Build | Ship a minimum viable version | What's the smallest thing that proves the idea? |
| Launch | Get it in front of people | Who are your first 10 customers? |
| Measure | Track what matters | Are people coming back? |
| Scale | Double down on what works | What's your highest-leverage channel? |

**Marketing & Sales Fundamentals**
- Know your ideal customer better than they know themselves
- Sell the transformation, not the feature list
- Profit follows retention -- a repeat customer is worth more than a new one

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll tailor this framework to your actual project and market.*"""

SANDBOX_LANGUAGE_ROADMAP = """### \U0001f5e3️ Your Language Learning Roadmap (Sandbox Preview)

1. **Absolute Basics (Week 1-2)**
   - Greetings, numbers, and common phrases
   - 15-20 minutes of daily listening practice

2. **Everyday Conversation (Week 3-4)**
   - Ordering food, asking directions, small talk
   - Start speaking out loud, even alone

3. **Grammar Foundations (Week 5-6)**
   - Core verb tenses and sentence structure
   - Read short, simple texts daily

4. **Real Immersion (Week 7+)**
   - Watch shows or podcasts in the target language
   - Find a conversation partner or tutor

**Quick tip:** Whether it's Arabic, English, or translation practice you need, consistency beats intensity -- 20 minutes daily outperforms 3 hours once a week.

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll tailor this roadmap to the language and level you're actually at.*"""

SANDBOX_STUDY_SCHEDULE = """### \U0001f4c5 Sample Weekly Study Schedule (Sandbox Preview)

| Day | Focus | Duration |
|---|---|---|
| Monday | Review lecture notes + flashcards | 45 min |
| Tuesday | Practice problems (weak topics) | 60 min |
| Wednesday | Watch next lecture + take notes | 45 min |
| Thursday | Practice problems (new topics) | 60 min |
| Friday | Light review + rest | 30 min |
| Saturday | Mock quiz / self-test | 60 min |
| Sunday | Rest, or a catch-up buffer | -- |

**Tips**
- Study in focused 25-45 minute blocks with short breaks in between.
- Spend 5-10 minutes revisiting yesterday's material before starting something new.
- Adjust this around your real exam dates once I'm fully connected!

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll build a schedule around your real courses and deadlines.*"""

SANDBOX_MATH_SCIENCE_GUIDE = """### \U0001f9ee Math & Science Concept Blueprint (Sandbox Preview)

Here's a general blueprint the real AI Coach can tailor to your exact problem set once it's connected:

1. **Identify the Core Concept**
   - What formula, law, or theorem is this problem built on?
   - Write it down before touching any numbers.

2. **Break the Problem into Knowns and Unknowns**
   - List every given value and what you're actually solving for.
   - Sketch a diagram if it's physics -- forces, vectors, or circuits become obvious fast.

3. **Apply the Formula Step-by-Step**
   - Substitute values one at a time; don't skip algebra steps.
   - Keep units attached the whole way through -- they catch mistakes before you do.

4. **Sanity-Check the Answer**
   - Does the magnitude make sense? (a car isn't going 3,000,000 m/s)
   - Re-derive from a different angle if you have time.

**Core Formula Reference**

| Field | Formula | What It Tells You |
|---|---|---|
| Algebra | x = (-b ± √(b²-4ac)) / 2a | Roots of a quadratic equation |
| Physics (Motion) | v = u + at | Velocity after constant acceleration |
| Calculus | d/dx [xⁿ] = nxⁿ⁻¹ | Power rule for derivatives |
| Chemistry | PV = nRT | Ideal gas law |

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll walk through your exact equation or concept step-by-step.*"""

SANDBOX_CAREER_GUIDE = """### \U0001f4bc Career & Interview Prep Kit (Sandbox Preview)

**Modern Resume/CV Structure**

| Section | What Goes Here |
|---|---|
| Header | Name, phone, email, LinkedIn/portfolio link |
| Summary | 2-3 lines on who you are + your strongest value |
| Experience | Bullet points starting with action verbs + a measurable result |
| Skills | Tools, languages, frameworks -- match the job posting's keywords |
| Education | Degree, institution, graduation year |

**Interview Prep Questions to Rehearse**
1. "Tell me about yourself" -- have a tight 60-second story ready.
2. "Tell me about a time you faced a conflict at work" -- use the STAR method (Situation, Task, Action, Result).
3. "Why do you want to work here?" -- show you've researched the company, not just the role.
4. "What's your biggest weakness?" -- pick something real, and show how you're actively improving it.

**Career Roadmap**

| Stage | Focus | Milestone |
|---|---|---|
| Foundation | Build core skills + a portfolio | 2-3 solid projects you can talk about in depth |
| Visibility | Network + apply strategically | 5-10 quality applications a week beats 50 generic ones |
| Interview | Practice out loud, not just in your head | A mock interview with a friend or mirror |
| Growth | Negotiate + keep learning | Always ask for feedback after an offer or rejection |

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll tailor this to your actual resume, target role, and industry.*"""

SANDBOX_DESIGN_GUIDE = """### \U0001f3a8 Design Fundamentals & Tool Roadmap (Sandbox Preview)

**Core Layout Rules**
- **Hierarchy first** -- the most important element should be the biggest, boldest, or closest.
- **Whitespace is a feature**, not empty space -- let elements breathe.
- **Alignment creates order** -- every element should line up with something else on the page.
- **Consistency builds trust** -- reuse the same spacing, corner radius, and type scale everywhere.

**Color Theory Quick Tips**

| Concept | Rule of Thumb |
|---|---|
| 60-30-10 rule | 60% dominant color, 30% secondary, 10% accent |
| Contrast | Text needs at least a 4.5:1 contrast ratio against its background |
| Complementary colors | Opposite on the color wheel -- great for accents, bad for large areas |
| Analogous colors | Neighboring on the wheel -- calm, cohesive palettes |

**Tool Learning Roadmap**
1. **Figma Basics (Week 1-2)** -- frames, auto layout, components
2. **Prototyping (Week 3)** -- linking screens, transitions, basic interactions
3. **Photoshop Fundamentals (Week 4-5)** -- layers, masks, and selections
4. **Design Systems (Week 6+)** -- building a reusable component library

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll tailor this to the tool and project you're actually working on.*"""

SANDBOX_PRODUCTIVITY_GUIDE = """### ⏱️ Focus & Productivity Framework (Sandbox Preview)

**The Pomodoro Method**
1. Work with full focus for 25 minutes (one task, phone away).
2. Take a 5-minute break -- stand up, stretch, look away from the screen.
3. After 4 pomodoros, take a longer 15-30 minute break.

**Time-Blocking, Step by Step**
- Block your calendar the night before -- don't plan your day the morning of.
- Batch similar tasks together (all emails in one block, not scattered all day).
- Protect at least one deep-work block (60-90 min, zero notifications) daily.

**Motivation Resets**

| When You Feel... | Try This |
|---|---|
| Overwhelmed | Write down every task, then pick just the top one |
| Unmotivated | Commit to just 5 minutes -- momentum usually takes over |
| Distracted | Put your phone in another room, not just face-down |
| Burnt out | Check if you've actually rested, not just stopped working |

*This is a sandbox preview -- once Mendoura AI Coach is fully connected, I'll build a schedule around your real deadlines and habits.*"""

SANDBOX_GENERAL_REPLY = (
    "**Hello!** I am your **Mendoura General AI Assistant** running in Sandbox mode. "
    "I can map out study strategies, break down complex concepts, or draft learning "
    "paths for you! What topic are we exploring today?"
)

# Keyword dictionaries for the sandbox intent-matching engine -- each pairs
# English and Arabic variations for the same real-world topic, since the
# platform serves both languages. Matched with word boundaries (see
# _matches_any) so short entries like "ui" or "cv" don't false-positive
# on substrings buried inside unrelated words (e.g. "build", "solve").
MATH_SCIENCE_KEYWORDS = ('math', 'physics', 'science', 'calculus', 'equation', 'رياضيات', 'فيزياء', 'علوم')
CAREER_KEYWORDS = ('job', 'resume', 'interview', 'career', 'cv', 'وظيفة', 'مقابلة', 'سيرة ذاتية')
DESIGN_KEYWORDS = ('ui', 'ux', 'design', 'photoshop', 'figma', 'colors', 'تصميم', 'فوتوشوب')
PRODUCTIVITY_KEYWORDS = ('focus', 'time management', 'motivation', 'تركيز', 'وقت', 'تنظيم', 'تحفيز')
TECH_KEYWORDS = ('python', 'js', 'javascript', 'html', 'code', 'bug', 'web')
BUSINESS_KEYWORDS = ('marketing', 'business', 'sales', 'profit', 'project')
LANGUAGE_KEYWORDS = ('english', 'arabic', 'translation', 'learn')
STUDY_KEYWORDS = ('study', 'schedule', 'exam')

# Welcoming variations for the catch-all fallback -- picked deterministically
# (by input length) rather than randomly, so the same question always gets
# the same reply, which keeps the sandbox predictable and testable.
CATCH_ALL_PREFIXES = (
    "That's a great question to dig into!",
    "Love the curiosity here!",
    "Great topic to explore together!",
    "Nice question -- let's break this down!",
    "Happy to help you think this through!",
)


class AICoachError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.AI_API_KEY)


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Word-boundary match so short keywords ("ui", "cv") don't trigger on
    substrings inside unrelated words ("build", "active")."""
    return any(re.search(rf'\b{re.escape(keyword)}\b', text) for keyword in keywords)


def _summarize_query(text: str) -> str:
    text = text.strip()
    if not text:
        return "what's on your mind"
    return text if len(text) <= 90 else text[:87].rstrip() + '...'


def _catch_all_reply(user_text: str) -> str:
    """Intelligent Catch-All Fallback Engine -- used whenever nothing in the
    topic dictionaries matches. Rather than a single static string, this
    parses the user's own sentence (its length, and whether it reads as a
    real question) to build a dynamic, encouraging mentorship reply that
    restates their curiosity and lays out a structured way to explore it."""
    prefix = CATCH_ALL_PREFIXES[len(user_text) % len(CATCH_ALL_PREFIXES)]
    snippet = _summarize_query(user_text)
    word_count = len(user_text.split())
    depth_note = (
        "Since that's a big, open-ended topic, let's zoom in with a structured approach:"
        if word_count > 6 else
        "Let's build a structured approach around it:"
    )

    return f"""**Hello!** I am your **Mendoura General AI Assistant** running in Sandbox mode. {prefix}

You asked about: *"{snippet}"* -- {depth_note}

### \U0001f9ed How to Structurally Analyze Any Topic

| Step | Focus | What To Do |
|---|---|---|
| 1. Deconstruct | Break it into its core parts | List the key terms, definitions, or sub-questions hiding inside your question |
| 2. Investigate | Study each part on its own | Look up the "why" behind each part before connecting them back together |
| 3. Synthesize & Apply | Rebuild the full picture | Explain it in your own words, then test that understanding with a real example |

**Quick tips while you explore "{snippet}":**
- Write a one-sentence summary in your own words -- if you can't, you've found the part to study next.
- Teach it to an imaginary student; explaining exposes gaps fast.
- Once Mendoura AI Coach is fully connected, I'll go deep on this exact topic with tailored explanations and practice.

*This is a sandbox preview -- ask me anything else and I'll adapt this same structure to it!*"""


def _sandbox_reply(history: list[dict]) -> str:
    """Keyword-matched canned reply used whenever AI_API_KEY isn't set.
    Checked most-specific-first across a global, bilingual (English/Arabic)
    intent dictionary; anything that matches nothing falls through to the
    dynamic catch-all mentorship engine below instead of a single static
    default string."""
    last_user_text = next(
        (m.get('content', '') for m in reversed(history) if m.get('role') == 'user'), ''
    )
    lowered = last_user_text.lower()

    if _matches_any(lowered, MATH_SCIENCE_KEYWORDS):
        return SANDBOX_MATH_SCIENCE_GUIDE
    if _matches_any(lowered, CAREER_KEYWORDS):
        return SANDBOX_CAREER_GUIDE
    if _matches_any(lowered, DESIGN_KEYWORDS):
        return SANDBOX_DESIGN_GUIDE
    if _matches_any(lowered, PRODUCTIVITY_KEYWORDS):
        return SANDBOX_PRODUCTIVITY_GUIDE
    if _matches_any(lowered, TECH_KEYWORDS):
        return SANDBOX_TECH_GUIDE
    if _matches_any(lowered, BUSINESS_KEYWORDS):
        return SANDBOX_BUSINESS_FRAMEWORK
    if _matches_any(lowered, LANGUAGE_KEYWORDS):
        return SANDBOX_LANGUAGE_ROADMAP
    if _matches_any(lowered, STUDY_KEYWORDS):
        return SANDBOX_STUDY_SCHEDULE
    return _catch_all_reply(last_user_text)


def send_message(history: list[dict]) -> str:
    """history is a list of {"role": "user"|"assistant", "content": str},
    oldest first. Returns the assistant's reply text -- a canned Sandbox
    Mode reply when AI_API_KEY isn't configured, so the chat stays usable
    instead of erroring."""
    if not is_configured():
        return _sandbox_reply(history)

    client = anthropic.Anthropic(api_key=settings.AI_API_KEY)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            thinking={'type': 'adaptive'},
            messages=history,
        )
    except anthropic.APIError as exc:
        raise AICoachError(str(exc)) from exc

    return ''.join(block.text for block in response.content if block.type == 'text').strip()
