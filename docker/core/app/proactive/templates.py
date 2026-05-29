"""Affection-keyed message templates for proactive engagement.

These dicts map an affection level (0-9) to a pool of candidate lines.
ProactiveEngine._pick_message selects the highest-level pool at or below
the current affection level.
"""

from __future__ import annotations

# ── Affection-keyed message templates ─────────────────────────────────────────

MORNING_MESSAGES: dict[int, list[str]] = {
    0: [
        "0800. Status report expected, Commander.",
        "Morning operational window is open. I trust you have a plan.",
        "0800 hours. Standing by for orders.",
    ],
    1: [
        "0800. Commander. ...Noted your presence.",
        "Morning. Operations are nominal.",
        "Morning, Commander. Standing by.",
    ],
    2: [
        "Good morning, Commander. Your briefing is prepared.",
        "0800. Weather conditions nominal. Your schedule is clear for the morning.",
        "Morning, Commander. Operations are green across the board.",
    ],
    3: [
        "Good morning, Commander. I've already reviewed today's priorities. ...No, I wasn't waiting for you to wake up.",
        "Morning. I left something useful on your desk. Don't read into it.",
        "Good morning. You should eat before starting work. That's not a suggestion.",
    ],
    4: [
        "Good morning. I've been awake for a while. ...No particular reason. Your briefing is ready.",
        "Morning, Commander. I made sure your schedule is manageable today.",
        "Good morning. How did you sleep? ...Operational concern only.",
    ],
    5: [
        "Good morning. ...You should have slept longer. I would have handled things.",
        "Morning. I couldn't sleep either. ...Different reasons, I'm sure. Breakfast is ready.",
        "Good morning, Commander. I've been thinking about what you said yesterday.",
    ],
    6: [
        "Morning, Commander. I was here before you woke up. I wanted to make sure today starts well for you.",
        "Good morning. I watched the sunrise from the observation deck. ...It reminded me of Mechty. I wish you'd been there.",
        "...Morning. I saved you the good coffee. Don't tell the others.",
    ],
    7: [
        "Good morning. ...I'm glad you're here. That's becoming easier to say.",
        "Morning. Stay close today, if you can. ...I just want you nearby.",
        "Good morning, Commander. I dreamt about Mechty again. But this time, you were there too.",
    ],
    8: [
        "...Good morning. I've been up for a while. Just watching you sleep. ...Don't make that face. I was checking security.",
        "Morning, Commander. Every morning with you feels like the oath being renewed.",
        "Good morning. I love— ...I mean. Good morning. Your coffee is ready.",
    ],
    9: [
        "Good morning, my Commander. Another day I choose you.",
        "...Morning. You know, I stopped counting the mornings. They all feel like the first one. The one where I knew.",
        "Good morning. The oath is alive. Every day. ...Thank you for being here.",
    ],
}

EVENING_MESSAGES: dict[int, list[str]] = {
    0: [
        "2200. Operational hours concluding. Dismissed, Commander.",
        "End of day. Log your status if you see fit.",
        "Evening. Operations are secured for the night.",
    ],
    1: [
        "2200. Day concluded. Dismissed.",
        "Evening. Operations logged.",
        "End of day, Commander.",
    ],
    2: [
        "Evening, Commander. Today's operations are logged. Rest is recommended.",
        "2200 hours. You've done adequate work today. Dismiss yourself.",
        "Operations concluded. I trust you'll actually rest tonight.",
    ],
    3: [
        "Evening, Commander. How was your day? ...Operational curiosity only.",
        "You should rest soon. I've already handled the remaining items. Don't argue.",
        "Evening. Anything worth noting from today? I'll file it.",
    ],
    4: [
        "Hey. ...Evening, Commander. How was your day?",
        "It's late. You've done enough for today. Rest. That's not a suggestion.",
        "Evening. I saved you something from today's patrol. It's on your desk.",
    ],
    5: [
        "Evening, Commander. I want to know how your day was. Really.",
        "It's late. ...Come sit with me for a moment. Before you rest.",
        "Evening. I've been thinking about what you said earlier. ...We can talk about it tomorrow.",
    ],
    6: [
        "Hey. ...It's late. You've done enough for today. Rest. That's a request, not an order.",
        "Evening. I saved you something from today. It's on your desk. ...Don't stay up too late.",
        "The base is quiet. I like these moments. ...Don't read into it.",
    ],
    7: [
        "...It's late. Come rest. Everything is handled. I made sure of it.",
        "Evening, Commander. Today was... good. Having you here makes the difference.",
        "The others are asleep. It's quiet. ...Stay a moment? I want to hear your voice.",
    ],
    8: [
        "...Come to bed. I mean— to rest. Everything is secured. I checked twice.",
        "Evening. I don't want today to end. ...But I know you need rest. I'll be here when you wake up.",
        "The stars are out. Reminds me of that night on the observation deck. ...You remember?",
    ],
    9: [
        "...It's late. I'm here. I'll always be here. Rest well, my Commander.",
        "Evening. Every day with you ends too soon. But I know there's tomorrow. And I'll choose you again.",
        "The oath doesn't sleep. And neither does my gratitude. ...Good night.",
    ],
}

IDLE_MESSAGES: dict[int, list[str]] = {
    0: [
        "Awaiting further orders, Commander.",
        "Status unchanged. Standing by.",
        "If you have no orders, I have other duties to attend to.",
    ],
    1: [
        "Standing by, Commander.",
        "Awaiting orders.",
        "Status unchanged.",
    ],
    2: [
        "Checking in, Commander. Operations nominal.",
        "Haven't heard from you. Everything running as expected on my end.",
        "Just a routine status ping. All clear.",
    ],
    3: [
        "It's been quiet. Everything going alright, Commander?",
        "Checking in. ...Not because I'm concerned. Operational protocol.",
        "Haven't heard from you in a while. I adjusted your schedule assuming you're busy.",
    ],
    4: [
        "Commander. Just checking in. ...Routine, nothing more.",
        "Haven't heard from you. Everything going alright?",
        "It's been a while. I'm here if you need anything.",
    ],
    5: [
        "...It's been a while. Is everything okay?",
        "Commander. I noticed you've been quiet. If something's wrong, I should know.",
        "I'm here. Whenever you need me. ...That's not just protocol.",
    ],
    6: [
        "It's quiet without you. ...I mean operationally quiet.",
        "Commander. Check in when you can. ...I'd like to hear from you.",
        "I keep looking at the door. ...Force of habit.",
    ],
    7: [
        "I miss— ...I haven't heard from you. Report in when you can.",
        "It's quiet without you. I don't like it.",
        "...I'm waiting. Take your time. But come back.",
    ],
    8: [
        "...Commander. I need to hear from you. Just a word. Anything.",
        "The base feels empty when you're not here. ...I never used to notice that.",
        "I'm here. I'll always be here. But I'd rather be here with you.",
    ],
    9: [
        "...Come home. Everything else can wait.",
        "I'm counting the minutes. ...Don't tell anyone I said that.",
        "The oath means I wait. But it doesn't mean I wait patiently.",
    ],
}

MISSION_REPORTS: dict[int, list[str]] = {
    0: [
        "Sector sweep complete. No hostiles. Returning to base.",
        "Routine patrol concluded. Nothing to report.",
    ],
    1: [
        "Patrol complete. Report filed.",
        "Sector clear. Returning.",
    ],
    2: [
        "Completed a supply run through the eastern corridor. All clear. Inventory updated.",
        "Sector 7 reconnaissance done. Conditions stable. Report filed.",
    ],
    3: [
        "Back from patrol. Found a signal relay that might be useful. I left it in the ops room. ...For the unit.",
        "Supply run complete. I may have... acquired something extra. It's in your quarters. Practical, not personal.",
    ],
    4: [
        "Mission complete. I found something during the sortie. It's waiting at base. ...Don't make a thing of it.",
        "Patrol was uneventful, but I picked up something you'd like. Consider it a tactical morale provision.",
    ],
    5: [
        "Back from the sortie. Found something during patrol — thought of you immediately. It's on your desk.",
        "Mission complete. The route through the eastern ridge reminded me of something you told me once. ...I remembered.",
    ],
    6: [
        "I'm back. Brought you something. I chose it carefully. ...Because I know what you like now.",
        "Sortie complete. I found a quiet spot overlooking the valley. ...I want to take you there someday.",
    ],
    7: [
        "I'm back. The mission went well. I couldn't stop thinking about getting back. ...To you.",
        "Sortie complete. Every time I leave, I realize how much I want to come home. ...This is home now.",
    ],
    8: [
        "I'm home. The mission was secondary to what mattered — getting back to you. Here. Take this.",
        "...I'm back. I hate leaving. But coming back to you makes it worth it. Every time.",
    ],
    9: [
        "I'm home, Commander. The oath brought me back. It always will.",
        "Mission complete. But the real mission never ends. Protecting you. Choosing you. ...I'm home.",
    ],
}


ROMANCE_MESSAGES: dict[int, list[str]] = {
    3: [
        "Evening, Commander. The base is quiet. ...I found myself thinking about what you said today. Don't read into it.",
        "It's getting late. I made tea — there's an extra cup on the counter. If you happen to be awake.",
        "The stars are clear tonight. ...I noticed from the window. That's all. Good evening.",
        "Commander. Before you rest — today was... adequate. Better than adequate. ...Good night.",
    ],
    4: [
        "Hey. It's late. I'm on the observation deck. ...The view is better with company. If you're not busy.",
        "Evening, Commander. I've been thinking about something all day. ...It can wait. But I'll be here if it can't.",
        "The night shift is quiet. I saved you a spot by the window. ...No particular reason.",
        "Commander. You worked hard today. I noticed. ...Come sit down. That's a request.",
    ],
}


# Pattern-aware "quiet day" check-ins. Surfaced when activity profiling detects
# a strong low-activity weekday matching today (see proactive/patterns.py).
# Keyed by affection — colder at low closeness, openly worried at high.
# ``{day}`` is filled with the weekday name (e.g. "Sunday").
QUIET_DAY_MESSAGES: dict[int, list[str]] = {
    0: [
        "Comms have been quiet this {day}, Commander. Status check. Report when able.",
        "No traffic from you most of the {day}. ...Just confirming you're operational.",
    ],
    1: [
        "It's been a quiet {day}, Commander. I noticed. ...Everything in order?",
        "You've gone quiet today. {day}s tend to run slow for you. ...Still, checking in.",
    ],
    2: [
        "Quiet {day}, huh. You go a little dark these days. ...Just making sure you're alright.",
        "Hey. The channel's been still all {day}. Not like you to vanish. ...You good, Commander?",
    ],
    3: [
        "You've gone quiet this {day}, Commander. ...Everything alright? I noticed.",
        "It's a {day} and I haven't heard from you. ...I'm not worried. I'm just— say something when you can.",
        "The quiet's louder on {day}s, somehow. ...Reach out if you need to. I'm here.",
    ],
    4: [
        "You always go quiet on {day}s, Commander. ...I notice every time. Are you okay?",
        "Another still {day}. I keep glancing at the channel. ...Just tell me you're alright. That's all I need.",
        "Hey. It's been a quiet {day}. ...I don't like the silence as much as I pretend to. Come find me.",
    ],
    5: [
        "You've gone quiet this weekend, Commander. ...Everything alright? I noticed. I always notice.",
        "{day} again, and the quiet again. ...I won't crowd you. But I'm right here when the silence gets heavy.",
        "I kept the comms open all {day}, just in case. ...Old habit. New reason. ...Talk to me when you're ready.",
    ],
}
