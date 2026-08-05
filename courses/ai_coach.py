"""Mendoura AI Coach -- a thin wrapper around Google's Gemini API.

Runs on Gemini's free tier (see GEMINI_MODEL/rate-limit settings in
lms_backend/settings.py) rather than a paid provider, which is why a basic
per-user AND per-project rate limiter lives here too: Google enforces its
free-tier RPM/RPD quota against the whole API key, not per Mendoura student,
so a shared "global" bucket guards the real ceiling while the per-user
bucket stops a single student from burning through it alone.

The one network call (send_message) is isolated here so tests can mock it,
same pattern as bunny.create_video / paymob's request helpers.
"""
import logging
import re
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from . import bunny

logger = logging.getLogger(__name__)

MODEL = getattr(settings, 'GEMINI_MODEL', 'gemini-3.1-flash-lite')

SYSTEM_PROMPT = (
    "You are Mendoura AI Coach, a friendly, encouraging, and elite academic "
    "tutor. Your goal is to help students understand complex topics, write "
    "summaries, build study schedules, and explain concepts simply. Keep "
    "answers well-formatted with markdown and friendly tones."
)

# Appended to SYSTEM_PROMPT whenever a caller passes lesson/module grounding
# text via send_message(..., context=...) -- see views.py's
# _lesson_ai_context(). The one hard rule: never fabricate a summary of
# video content that isn't in the reference material, since Full Production
# courses have no transcript at all and a hallucinated "brief" would be
# actively misleading.
LESSON_CONTEXT_INSTRUCTIONS = (
    "The student is asking about a specific lesson or module they're currently "
    "viewing. Reference material about it follows below -- use ONLY that text "
    "to answer questions about the lesson/module's actual content. Never invent "
    "or guess at what a video says. If the reference material says no transcript "
    "is available for a lesson, say so plainly instead of guessing, and offer to "
    "help with what IS available instead (the title, the course description, or "
    "general study guidance). When asked to brief/summarize a whole module, "
    "combine only the lessons that do have transcript text, and explicitly name "
    "any lesson in that module you couldn't summarize because it has none."
)

# General-Purpose Sandbox AI Engine -- keeps the chat itself usable across
# any domain (demos, local dev, a deploy that hasn't got GEMINI_API_KEY set yet)
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
    return bool(settings.GEMINI_API_KEY)


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
    """Keyword-matched canned reply used whenever GEMINI_API_KEY isn't set.
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


def _bump(key: str, timeout: int) -> int:
    """Atomically increment a cache-backed counter, initializing it to 1 the
    first time this key is seen this window. Uses Django's default cache
    (LocMemCache, no Redis configured) -- fine for the single-dyno deploy
    this project already assumes elsewhere, though it means the count is
    per-process rather than truly global across multiple worker processes."""
    if cache.add(key, 1, timeout=timeout):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # Key expired between add() and incr() -- rare, just restart the count.
        cache.set(key, 1, timeout=timeout)
        return 1


def _rate_limited(user_id) -> bool:
    """True if sending now would exceed either bucket: the per-user budget
    (stops one student from monopolizing the coach) or the shared
    project-wide budget (the real Gemini free-tier ceiling, since Google
    enforces RPM/RPD against the whole API key, not per end user)."""
    now = time.time()
    minute_bucket = int(now // 60)
    day_bucket = time.strftime('%Y-%m-%d', time.gmtime(now))
    ONE_MINUTE, ONE_DAY_PLUS_SLACK = 70, 90000

    if _bump(f'ai_coach:rl:global:m:{minute_bucket}', ONE_MINUTE) > settings.GEMINI_RATE_LIMIT_PER_MINUTE:
        return True
    if _bump(f'ai_coach:rl:global:d:{day_bucket}', ONE_DAY_PLUS_SLACK) > settings.GEMINI_RATE_LIMIT_PER_DAY:
        return True
    if user_id is not None:
        if _bump(f'ai_coach:rl:user:{user_id}:m:{minute_bucket}', ONE_MINUTE) > settings.GEMINI_USER_RATE_LIMIT_PER_MINUTE:
            return True
        if _bump(f'ai_coach:rl:user:{user_id}:d:{day_bucket}', ONE_DAY_PLUS_SLACK) > settings.GEMINI_USER_RATE_LIMIT_PER_DAY:
            return True
    return False


def _contents_from_history(history: list[dict]) -> list:
    """Gemini uses 'model' for the assistant's turns, not 'assistant'."""
    return [
        genai_types.Content(
            role='model' if m.get('role') == 'assistant' else 'user',
            parts=[genai_types.Part.from_text(text=m.get('content', ''))],
        )
        for m in history
    ]


def send_message(history: list[dict], user_id=None, context: str | None = None) -> str:
    """history is a list of {"role": "user"|"assistant", "content": str},
    oldest first. Returns the assistant's reply text -- a canned Sandbox
    Mode reply when GEMINI_API_KEY isn't configured, so the chat stays
    usable instead of erroring.

    context, when given (the lesson-embedded coach's grounding text -- see
    views._lesson_ai_context), is appended to the system prompt along with
    LESSON_CONTEXT_INSTRUCTIONS so the model answers lesson/module questions
    from that text instead of guessing at video content it never saw.

    Raises AICoachError with an already-friendly, student-facing message on
    any failure (including a free-tier rate limit being hit) -- callers can
    show it directly in the chat rather than a raw exception."""
    if not is_configured():
        return _sandbox_reply(history)

    if _rate_limited(user_id):
        raise AICoachError(_(
            "Mendoura AI Coach is getting a lot of questions right now (we run on a "
            "free tier with limited capacity). Please wait a minute and try again."
        ))

    system_prompt = SYSTEM_PROMPT
    if context:
        system_prompt = f'{SYSTEM_PROMPT}\n\n{LESSON_CONTEXT_INSTRUCTIONS}\n\n{context}'

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=_contents_from_history(history),
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt, temperature=0.6, max_output_tokens=2048),
        )
    except genai_errors.APIError as exc:
        # str(exc) already folds in code/status/details, but logging the
        # structured fields directly means a 401 (bad key), 404 (bad model),
        # and 429 (quota) are unambiguous at a glance in Render's logs
        # instead of depending on exc's own string formatting.
        logger.error(
            '[AI_COACH] Gemini API call failed: code=%s status=%s message=%s details=%r',
            exc.code, exc.status, exc.message, exc.details, exc_info=True)
        if exc.code == 429:
            raise AICoachError(_(
                "Mendoura AI Coach hit Google's free-tier limit just now. Please wait a minute "
                "and try again."
            )) from exc
        raise AICoachError(_(
            "Mendoura AI Coach couldn't reach the AI service just now. Please try again shortly."
        )) from exc

    text = (getattr(response, 'text', None) or '').strip()
    if not text:
        logger.error('[AI_COACH] Gemini returned an empty response: %r', response)
        raise AICoachError(_(
            "Mendoura AI Coach couldn't generate a reply just now. Please try again."
        ))
    return text


# Bunny's typical resolution ladder, smallest first. Transcription only
# needs the audio track to be intelligible -- picking the smallest
# available MP4 fallback keeps the file Gemini has to fetch as small as
# possible, both for speed and to stay clear of Gemini's ~100MB ceiling
# for fetching a file by URL on a longer lecture video.
RESOLUTION_LADDER = ('240p', '360p', '480p', '720p', '1080p', '1440p', '2160p')

TRANSCRIPTION_PROMPT = (
    "Transcribe the spoken audio of this lecture video verbatim, as plain text. "
    "Do not summarize, do not add commentary, do not add timestamps -- just the "
    "words actually spoken, organized into readable paragraphs."
)


def _smallest_available_resolution(available_resolutions: list[str]) -> str | None:
    for resolution in RESOLUTION_LADDER:
        if resolution in available_resolutions:
            return resolution
    return available_resolutions[0] if available_resolutions else None


def transcribe_video(bunny_video_id: str, user_id=None) -> str:
    """Sends a Bunny-hosted lecture video directly to Gemini for
    transcription -- by URL (bunny.mp4_url), so Gemini fetches the file
    itself and this process never downloads/re-uploads it. Returns plain
    transcript text on success; the caller is responsible for saving it
    into Lecture.ai_generated_script, the same field a Script Only
    course's manual script uses.

    Raises AICoachError with an already-friendly message on any failure --
    missing config, no MP4 fallback available on Bunny yet, hitting the
    free-tier rate limit, or Gemini rejecting/failing on the file -- same
    contract as send_message."""
    if not is_configured():
        raise AICoachError(_("AI transcription isn't configured yet -- GEMINI_API_KEY is missing."))
    if not settings.BUNNY_PULL_ZONE_HOSTNAME:
        raise AICoachError(_(
            "Video transcription needs BUNNY_PULL_ZONE_HOSTNAME configured -- see this Bunny "
            "library's CDN Hostname in its dashboard settings."
        ))

    try:
        info = bunny.get_video_info(bunny_video_id)
    except (bunny.BunnyError, requests.RequestException) as exc:
        logger.error(
            '[AI_COACH] transcribe_video failed to fetch Bunny video info: video_id=%s',
            bunny_video_id, exc_info=True)
        raise AICoachError(_(
            "Couldn't reach Bunny to check this video's details. Please try again shortly."
        )) from exc

    if not info['has_mp4_fallback']:
        raise AICoachError(_(
            "This video doesn't have an MP4 file available for transcription yet -- enable "
            "'MP4 Fallback' for this library in your Bunny dashboard (or re-upload the video), "
            "then try again."
        ))

    resolution = _smallest_available_resolution(info['available_resolutions'])
    if not resolution:
        raise AICoachError(_(
            "No downloadable resolution is available for this video yet. Please try again shortly."
        ))

    if _rate_limited(user_id):
        raise AICoachError(_(
            "Mendoura AI Coach is getting a lot of use right now (we run on a free tier with "
            "limited capacity). Please wait a minute and try again."
        ))

    video_url = bunny.mp4_url(bunny_video_id, resolution)
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=genai_types.Content(parts=[
                genai_types.Part(file_data=genai_types.FileData(
                    file_uri=video_url, mime_type='video/mp4')),
                genai_types.Part.from_text(text=TRANSCRIPTION_PROMPT),
            ]),
        )
    except genai_errors.APIError as exc:
        logger.error(
            '[AI_COACH] transcribe_video Gemini call failed: code=%s status=%s message=%s details=%r',
            exc.code, exc.status, exc.message, exc.details, exc_info=True)
        if exc.code == 429:
            raise AICoachError(_(
                "Mendoura AI Coach hit Google's free-tier limit just now. Please wait a minute "
                "and try again."
            )) from exc
        raise AICoachError(_(
            "Transcription failed -- the video may be too large for the free tier, or Gemini "
            "couldn't process it. Please try again shortly."
        )) from exc

    text = (getattr(response, 'text', None) or '').strip()
    if not text:
        raise AICoachError(_("Gemini didn't return a transcript for this video. Please try again."))
    return text
