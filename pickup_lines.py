"""Pickup-line library for the Pickup Lines page.

Each line carries one or more category tags, not a single category:
a line is usually spoken in one situation but fits several — e.g. a
Monday-morning reefer line is a Monday line, a Morning line and a
Reefer line at once. Forcing one category per line would mean either
duplicating the line under each heading or filing it under an
arbitrary one and hiding it from the other filters, so tags win.
The first tag is the line's primary category (used as ``category``
in the reference API for backward compatibility).

Categories are an explicit ordered list (display order), not a
sorted set, so weekdays read Monday-Friday and related groups sit
together. CATEGORY_GROUPS drives the grouped checkboxes on the
owner's add-line form.
"""

PICKUP_LINES = [
    # Weekday — Friday (spoken Friday, about Monday loads)
    (("Friday",), "Hi, this is Zack from RoadxSolutions — are you set for next week, or do you need a load for Monday?"),
    (("Friday", "Weekend"), "It's Friday — most Monday loads get booked before the weekend. Want me to line one up for you?"),
    (("Friday",), "Quick one before the weekend: where will your truck be empty on Monday morning?"),
    # Weekday — Monday
    (("Monday", "Morning"), "Good morning — do you need a load for today? Send me the zip where your truck is empty and I'll check what's around."),
    (("Monday",), "Hi, this is Zack. Are you on a load right now, or are you empty and looking?"),
    (("Monday",), "Monday loads move fast — where are you empty and I'll find you something before the day slips away?"),
    # Weekday — Tuesday
    (("Tuesday",), "Tuesday check-in — is your truck empty today, or already loaded for the week?"),
    (("Tuesday",), "Hi, this is Zack. Mid-week freight is posting — where is your truck empty and I'll check what's near you?"),
    # Weekday — Wednesday
    (("Wednesday",), "Wednesday — halfway through the week. Is your truck free for a load before the weekend?"),
    (("Wednesday",), "Hi, this is Zack. Got freight moving this week — where will your truck be empty tomorrow?"),
    # Weekday — Thursday
    (("Thursday",), "Thursday — most Friday loads book today. Is your truck empty, and where?"),
    (("Thursday",), "Hi, this is Zack. Planning your weekend run? Tell me where you'll be empty on Friday and I'll line something up."),
    # Weekend
    (("Weekend", "Monday"), "Weekend check-in: where will your truck be empty on Monday? I'll line up loads so you don't start the week sitting."),
    (("Weekend",), "Hi, this is Zack. Most Monday freight books over the weekend — want me to hold something for you?"),
    # Time of day
    (("Morning",), "Good morning, this is Zack from RoadxSolutions. Got your truck empty today? Tell me the zip and I'll check loads near you."),
    (("Morning",), "Morning — quick question, is your truck rolling today or sitting empty?"),
    (("Afternoon",), "Afternoon — checking in. Is your truck empty this afternoon, or already loaded?"),
    (("Afternoon",), "Hi, this is Zack. Your truck free later today? I can check what's posting near your zip."),
    (("Evening",), "Evening — planning tomorrow? Tell me where you'll be empty and I'll have loads ready first thing."),
    (("Evening",), "Quick call before the day ends — do you need a load for tomorrow morning?"),
    # Truck / cargo type
    (("Reefer",), "Hi, this is Zack. I have reefer freight moving this week — is your reefer empty, and where?"),
    (("Reefer",), "Reefer loads pay well when the truck is cold and ready. Where is yours sitting right now?"),
    (("Box Truck",), "Hi, this is Zack. I have box truck freight posting — is your box truck empty, and where?"),
    (("Box Truck",), "Box truck loads move quick — is yours free today? Send me the zip and I'll check."),
    (("Small Trucks",), "Hi, this is Zack. Got loads that fit smaller trucks — is yours empty, and where?"),
    (("Small Trucks",), "Small truck freight is posting — is your truck free today? Tell me the zip."),
    (("Fresh", "Cargo"), "Hi — got fresh produce loads posting. Is your truck free for a produce run?"),
    (("Fresh", "Cargo"), "Produce moves fast and pays fast. Where will your truck be empty?"),
    (("Cargo",), "Hi, this is Zack from RoadxSolutions. Got general freight in your lane — is your truck empty?"),
    (("Cargo",), "Quick question — do you need a general freight load? Send the zip and I'll check."),
    # Situation
    (("Urgent Load",), "Hi — got an urgent load that needs to move now. Is your truck free?"),
    (("Urgent Load",), "This one pays for speed — truck empty and ready to roll right now?"),
    (("Under-MC",), "Hi, this is Zack. If you need to run under an MC, we can set you up and find you loads — is your truck ready to roll?"),
    (("Under-MC",), "Hi — do you have a truck but no active MC of your own? We can run you under ours and keep you loaded. Want the details?"),
    (("MC Expired",), "Hi, this is Zack. If your MC has expired, we can still get you running — want me to walk you through it?"),
    (("MC Expired",), "Hi — an expired MC doesn't have to mean a parked truck. We can help you get back on the road and find loads. Interested?"),
    (("Insurance Issue",), "Hi, this is Zack. Insurance trouble holding your truck back? We can talk through options and get you loaded once it's sorted."),
    (("Insurance Issue",), "Hi — if an insurance issue has you parked, give me a call when it's cleared and I'll have loads ready. Or want me to explain how we work meanwhile?"),
    # Conversation
    (("Follow-up",), "Hi, just following up — did you get a chance to think about running with us?"),
    (("Follow-up",), "Hi, it's Zack again. No pressure — just checking if your truck is free this week."),
    (("Follow-up",), "Following up on our last chat — is your truck still empty, or did you get loaded?"),
    (("First Contact",), "Hi, this is Zack from RoadxSolutions. We find loads for carriers before they even hit the load boards — do you have a truck running right now?"),
    (("First Contact",), "Hi — quick question. Who finds your loads right now, and are you happy with the rates?"),
    (("First Contact",), "Hi, this is Zack. We dispatch for carriers like you — one week with us and you can judge us on our work. Got a truck free?"),
]

# Display order for the filter chips and the add-line form.
CATEGORIES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Weekend",
    "Morning", "Afternoon", "Evening",
    "Reefer", "Box Truck", "Small Trucks", "Cargo", "Fresh",
    "Urgent Load", "Under-MC", "MC Expired", "Insurance Issue",
    "First Contact", "Follow-up",
]

# Grouped headings for the add-line form's category checkboxes.
CATEGORY_GROUPS = [
    ("Weekday", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
    ("Weekend", ["Weekend"]),
    ("Time of day", ["Morning", "Afternoon", "Evening"]),
    ("Truck / cargo", ["Reefer", "Box Truck", "Small Trucks", "Cargo", "Fresh"]),
    ("Situation", ["Urgent Load", "Under-MC", "MC Expired", "Insurance Issue"]),
    ("Conversation", ["First Contact", "Follow-up"]),
]


def lines_for(category=None):
    """All lines, or only the ones tagged with ``category``."""
    if not category:
        return PICKUP_LINES
    return [(tags, t) for tags, t in PICKUP_LINES if category in tags]
