"""Pickup-line library for the Pickup Lines page."""

PICKUP_LINES = [
    ("Friday", "Hi, this is Zack from RoadxSolutions — are you set for next week, or do you need a load for Monday?"),
    ("Friday", "It's Friday — most Monday loads get booked before the weekend. Want me to line one up for you?"),
    ("Friday", "Quick one before the weekend: where will your truck be empty on Monday morning?"),
    ("Monday", "Good morning — do you need a load for today? Send me the zip where your truck is empty and I'll check what's around."),
    ("Monday", "Hi, this is Zack. Are you on a load right now, or are you empty and looking?"),
    ("Monday", "Monday loads move fast — where are you empty and I'll find you something before the day slips away?"),
    ("Morning", "Good morning, this is Zack from RoadxSolutions. Got your truck empty today? Tell me the zip and I'll check loads near you."),
    ("Morning", "Morning — quick question, is your truck rolling today or sitting empty?"),
    ("Afternoon", "Afternoon — checking in. Is your truck empty this afternoon, or already loaded?"),
    ("Afternoon", "Hi, this is Zack. Your truck free later today? I can check what's posting near your zip."),
    ("Evening", "Evening — planning tomorrow? Tell me where you'll be empty and I'll have loads ready first thing."),
    ("Evening", "Quick call before the day ends — do you need a load for tomorrow morning?"),
    ("Weekend", "Weekend check-in: where will your truck be empty on Monday? I'll line up loads so you don't start the week sitting."),
    ("Weekend", "Hi, this is Zack. Most Monday freight books over the weekend — want me to hold something for you?"),
    ("Reefer", "Hi, this is Zack. I have reefer freight moving this week — is your reefer empty, and where?"),
    ("Reefer", "Reefer loads pay well when the truck is cold and ready. Where is yours sitting right now?"),
    ("Fresh", "Hi — got fresh produce loads posting. Is your truck free for a produce run?"),
    ("Fresh", "Produce moves fast and pays fast. Where will your truck be empty?"),
    ("General Freight", "Hi, this is Zack from RoadxSolutions. Got general freight in your lane — is your truck empty?"),
    ("General Freight", "Quick question — do you need a general freight load? Send the zip and I'll check."),
    ("Emergency Load", "Hi — got an urgent load that needs to move now. Is your truck free?"),
    ("Emergency Load", "This one pays for speed — truck empty and ready to roll right now?"),
    ("Follow-up", "Hi, just following up — did you get a chance to think about running with us?"),
    ("Follow-up", "Hi, it's Zack again. No pressure — just checking if your truck is free this week."),
    ("Follow-up", "Following up on our last chat — is your truck still empty, or did you get loaded?"),
    ("First Contact", "Hi, this is Zack from RoadxSolutions. We find loads for carriers before they even hit the load boards — do you have a truck running right now?"),
    ("First Contact", "Hi — quick question. Who finds your loads right now, and are you happy with the rates?"),
    ("First Contact", "Hi, this is Zack. We dispatch for carriers like you — one week with us and you can judge us on our work. Got a truck free?"),
]

CATEGORIES = sorted({c for c, _ in PICKUP_LINES})


def lines_for(category=None):
    if not category:
        return PICKUP_LINES
    return [(c, t) for c, t in PICKUP_LINES if c == category]
