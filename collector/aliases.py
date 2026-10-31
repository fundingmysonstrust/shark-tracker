"""
Team names and aliases, so "USC", "Southern California" and "Trojans" all
resolve to the same team.

Format of each line in the tables below:

    canonical_code | Full Name | alias, alias, alias ...

Every alias is matched case-insensitively after punctuation is removed.
An alias that would point to two different teams in the same league
(for example "new york" in the NFL) is dropped automatically so it can
never cause a wrong match.

At runtime `TeamBook.add()` can also learn extra names from the platforms'
own team lists (Polymarket publishes one), which covers teams not listed
here. To add a missing alias by hand, add it to the end of the right line.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

NFL = """
ari|Arizona Cardinals|arizona, cardinals, ari, arz
atl|Atlanta Falcons|atlanta, falcons, atl
bal|Baltimore Ravens|baltimore, ravens, bal, blt
buf|Buffalo Bills|buffalo, bills, buf
car|Carolina Panthers|carolina, panthers, car
chi|Chicago Bears|chicago, bears, chi
cin|Cincinnati Bengals|cincinnati, bengals, cin
cle|Cleveland Browns|cleveland, browns, cle, clv
dal|Dallas Cowboys|dallas, cowboys, dal
den|Denver Broncos|denver, broncos, den
det|Detroit Lions|detroit, lions, det
gb|Green Bay Packers|green bay, packers, gb, gnb
hou|Houston Texans|houston, texans, hou, hst
ind|Indianapolis Colts|indianapolis, colts, ind
jax|Jacksonville Jaguars|jacksonville, jaguars, jax, jac
kc|Kansas City Chiefs|kansas city, chiefs, kc, kan
lv|Las Vegas Raiders|las vegas, raiders, lv, lvr, oak
lac|Los Angeles Chargers|chargers, lac, la chargers, los angeles c
lar|Los Angeles Rams|rams, lar, la rams, los angeles r, la
mia|Miami Dolphins|miami, dolphins, mia
min|Minnesota Vikings|minnesota, vikings, min
ne|New England Patriots|new england, patriots, ne, nwe
no|New Orleans Saints|new orleans, saints, no, nor
nyg|New York Giants|giants, nyg, ny giants, new york g
nyj|New York Jets|jets, nyj, ny jets, new york j
phi|Philadelphia Eagles|philadelphia, eagles, phi
pit|Pittsburgh Steelers|pittsburgh, steelers, pit
sf|San Francisco 49ers|san francisco, 49ers, niners, sf, sfo
sea|Seattle Seahawks|seattle, seahawks, sea
tb|Tampa Bay Buccaneers|tampa bay, buccaneers, bucs, tb, tam
ten|Tennessee Titans|tennessee, titans, ten
was|Washington Commanders|washington, commanders, was, wsh, wash
"""

NBA = """
atl|Atlanta Hawks|atlanta, hawks, atl
bos|Boston Celtics|boston, celtics, bos
bkn|Brooklyn Nets|brooklyn, nets, bkn, bk, brk
cha|Charlotte Hornets|charlotte, hornets, cha, cho
chi|Chicago Bulls|chicago, bulls, chi
cle|Cleveland Cavaliers|cleveland, cavaliers, cavs, cle
dal|Dallas Mavericks|dallas, mavericks, mavs, dal
den|Denver Nuggets|denver, nuggets, den
det|Detroit Pistons|detroit, pistons, det
gsw|Golden State Warriors|golden state, warriors, gsw, gs
hou|Houston Rockets|houston, rockets, hou
ind|Indiana Pacers|indiana, pacers, ind
lac|LA Clippers|clippers, lac, la clippers, los angeles clippers
lal|Los Angeles Lakers|lakers, lal, la lakers
mem|Memphis Grizzlies|memphis, grizzlies, mem
mia|Miami Heat|miami, heat, mia
mil|Milwaukee Bucks|milwaukee, bucks, mil
min|Minnesota Timberwolves|minnesota, timberwolves, wolves, min
nop|New Orleans Pelicans|new orleans, pelicans, nop, no, nor
nyk|New York Knicks|knicks, nyk, ny, new york
okc|Oklahoma City Thunder|oklahoma city, thunder, okc
orl|Orlando Magic|orlando, magic, orl
phi|Philadelphia 76ers|philadelphia, 76ers, sixers, phi
phx|Phoenix Suns|phoenix, suns, phx, pho
por|Portland Trail Blazers|portland, trail blazers, blazers, por
sac|Sacramento Kings|sacramento, kings, sac
sas|San Antonio Spurs|san antonio, spurs, sas, sa
tor|Toronto Raptors|toronto, raptors, tor
uta|Utah Jazz|utah, jazz, uta, utah jazz
was|Washington Wizards|washington, wizards, was, wsh
"""

MLB = """
ari|Arizona Diamondbacks|arizona, diamondbacks, dbacks, ari, az
atl|Atlanta Braves|atlanta, braves, atl
bal|Baltimore Orioles|baltimore, orioles, bal
bos|Boston Red Sox|boston, red sox, bos
chc|Chicago Cubs|cubs, chc
cws|Chicago White Sox|white sox, cws, chw
cin|Cincinnati Reds|cincinnati, reds, cin
cle|Cleveland Guardians|cleveland, guardians, cle
col|Colorado Rockies|colorado, rockies, col
det|Detroit Tigers|detroit, tigers, det
hou|Houston Astros|houston, astros, hou
kc|Kansas City Royals|kansas city, royals, kc, kcr
laa|Los Angeles Angels|angels, laa, la angels
lad|Los Angeles Dodgers|dodgers, lad, la dodgers
mia|Miami Marlins|miami, marlins, mia
mil|Milwaukee Brewers|milwaukee, brewers, mil
min|Minnesota Twins|minnesota, twins, min
nym|New York Mets|mets, nym, ny mets
nyy|New York Yankees|yankees, nyy, ny yankees
ath|Athletics|athletics, as, ath, oak, oakland
phi|Philadelphia Phillies|philadelphia, phillies, phi
pit|Pittsburgh Pirates|pittsburgh, pirates, pit
sd|San Diego Padres|san diego, padres, sd, sdp
sf|San Francisco Giants|san francisco, sf giants, sf, sfg
sea|Seattle Mariners|seattle, mariners, sea
stl|St. Louis Cardinals|st louis, saint louis, stl
tb|Tampa Bay Rays|tampa bay, rays, tb, tbr
tex|Texas Rangers|texas, tex
tor|Toronto Blue Jays|toronto, blue jays, jays, tor
wsh|Washington Nationals|washington, nationals, nats, wsh, was
"""

NHL = """
ana|Anaheim Ducks|anaheim, ducks, ana
bos|Boston Bruins|boston, bruins, bos
buf|Buffalo Sabres|buffalo, sabres, buf
cgy|Calgary Flames|calgary, flames, cgy
car|Carolina Hurricanes|carolina, hurricanes, canes, car
chi|Chicago Blackhawks|chicago, blackhawks, chi
col|Colorado Avalanche|colorado, avalanche, avs, col
cbj|Columbus Blue Jackets|columbus, blue jackets, cbj
dal|Dallas Stars|dallas, stars, dal
det|Detroit Red Wings|detroit, red wings, det
edm|Edmonton Oilers|edmonton, oilers, edm
fla|Florida Panthers|florida, fla
lak|Los Angeles Kings|la kings, lak, la
min|Minnesota Wild|minnesota, wild, min
mtl|Montreal Canadiens|montreal, canadiens, habs, mtl, mon
nsh|Nashville Predators|nashville, predators, preds, nsh
njd|New Jersey Devils|new jersey, devils, njd, nj
nyi|New York Islanders|islanders, nyi
nyr|New York Rangers|ny rangers, nyr
ott|Ottawa Senators|ottawa, senators, ott
phi|Philadelphia Flyers|philadelphia, flyers, phi
pit|Pittsburgh Penguins|pittsburgh, penguins, pens, pit
sjs|San Jose Sharks|san jose, sjs, sj
sea|Seattle Kraken|seattle, kraken, sea
stl|St. Louis Blues|st louis, saint louis, blues, stl
tbl|Tampa Bay Lightning|tampa bay, lightning, tbl, tb
tor|Toronto Maple Leafs|toronto, maple leafs, leafs, tor
uta|Utah Mammoth|utah, mammoth, utah hockey club, uta
van|Vancouver Canucks|vancouver, canucks, van
vgk|Vegas Golden Knights|vegas, golden knights, vgk, veg
wsh|Washington Capitals|washington, capitals, caps, wsh, was
wpg|Winnipeg Jets|winnipeg, wpg
"""

# College: the same table serves college football (cfb) and college
# basketball (cbb). Nicknames shared by many schools (e.g. "tigers",
# "bulldogs") are left out on purpose; they would be ambiguous.
COLLEGE = """
usc|USC Trojans|usc, southern california, southern cal, trojans, so cal
ore|Oregon Ducks|oregon, ore, ducks
osu|Ohio State Buckeyes|ohio state, osu, ohst, buckeyes, ohio st
mich|Michigan Wolverines|michigan, mich, um, wolverines
msu|Michigan State Spartans|michigan state, msu, mist, spartans, michigan st
psu|Penn State Nittany Lions|penn state, psu, pennst, nittany lions, penn st
bama|Alabama Crimson Tide|alabama, bama, ala, crimson tide
uga|Georgia Bulldogs|georgia, uga, geo
lsu|LSU Tigers|lsu, louisiana state
aub|Auburn Tigers|auburn, aub
fla|Florida Gators|florida, fla, uf, gators
fsu|Florida State Seminoles|florida state, fsu, flst, seminoles, florida st
mia|Miami Hurricanes|miami fl, miami (fl), miami florida, mia, um hurricanes
tex|Texas Longhorns|texas, tex, longhorns, ut austin
tamu|Texas A&M Aggies|texas a&m, texas am, tamu, txam
ou|Oklahoma Sooners|oklahoma, ou, okla, sooners
okst|Oklahoma State Cowboys|oklahoma state, okst, osu cowboys, oklahoma st
nd|Notre Dame Fighting Irish|notre dame, nd, fighting irish
clem|Clemson Tigers|clemson, clem
tenn|Tennessee Volunteers|tennessee, tenn, vols, volunteers
ole|Ole Miss Rebels|ole miss, mississippi, miss, ole
msst|Mississippi State Bulldogs|mississippi state, msst, miss st, mississippi st
ark|Arkansas Razorbacks|arkansas, ark, razorbacks
uk|Kentucky Wildcats|kentucky, uk, ken, ky
sc|South Carolina Gamecocks|south carolina, scar, gamecocks
mizz|Missouri Tigers|missouri, mizz, mizzou, miz
vand|Vanderbilt Commodores|vanderbilt, vandy, vand, commodores
wash|Washington Huskies|washington, wash, uw, huskies
wsu|Washington State Cougars|washington state, wsu, wazzu, washington st
ucla|UCLA Bruins|ucla, bruins
stan|Stanford Cardinal|stanford, stan
cal|California Golden Bears|california, cal, golden bears
utah|Utah Utes|utah, utes
colo|Colorado Buffaloes|colorado, colo, buffaloes, buffs
asu|Arizona State Sun Devils|arizona state, asu, sun devils, arizona st
ariz|Arizona Wildcats|arizona, ariz, ua
byu|BYU Cougars|byu, brigham young
tcu|TCU Horned Frogs|tcu, texas christian, horned frogs
bay|Baylor Bears|baylor, bay
ttu|Texas Tech Red Raiders|texas tech, ttu, red raiders
ku|Kansas Jayhawks|kansas, ku, jayhawks
ksu|Kansas State Wildcats|kansas state, ksu, kstate, k state, kansas st
isu|Iowa State Cyclones|iowa state, isu, cyclones, iowa st
iowa|Iowa Hawkeyes|iowa, hawkeyes
wvu|West Virginia Mountaineers|west virginia, wvu
cin|Cincinnati Bearcats|cincinnati, cin, bearcats
hou|Houston Cougars|houston, hou
ucf|UCF Knights|ucf, central florida
wis|Wisconsin Badgers|wisconsin, wis, wisc, badgers
minn|Minnesota Golden Gophers|minnesota, minn, gophers, golden gophers
neb|Nebraska Cornhuskers|nebraska, neb, cornhuskers, huskers
ill|Illinois Fighting Illini|illinois, ill, illini
nw|Northwestern Wildcats|northwestern, nw, nwu
pur|Purdue Boilermakers|purdue, pur, boilermakers
ind|Indiana Hoosiers|indiana, ind, iu, hoosiers
md|Maryland Terrapins|maryland, md, terps, terrapins
rut|Rutgers Scarlet Knights|rutgers, rut, scarlet knights
unc|North Carolina Tar Heels|north carolina, unc, tar heels
ncst|NC State Wolfpack|nc state, north carolina state, ncst, ncsu, wolfpack
duke|Duke Blue Devils|duke, blue devils
wake|Wake Forest Demon Deacons|wake forest, wake, demon deacons
uva|Virginia Cavaliers|virginia, uva
vt|Virginia Tech Hokies|virginia tech, vt, hokies
lou|Louisville Cardinals|louisville, lou, lville
pitt|Pittsburgh Panthers|pittsburgh, pitt
syr|Syracuse Orange|syracuse, syr, cuse
bc|Boston College Eagles|boston college, bc
gt|Georgia Tech Yellow Jackets|georgia tech, gt, gatech, yellow jackets
smu|SMU Mustangs|smu, southern methodist, mustangs
uconn|UConn Huskies|uconn, connecticut, conn
gonz|Gonzaga Bulldogs|gonzaga, gonz, zags
vill|Villanova Wildcats|villanova, vill, nova
mar|Marquette Golden Eagles|marquette, marq
crei|Creighton Bluejays|creighton, crei
xav|Xavier Musketeers|xavier, xav
stj|St. John's Red Storm|st johns, saint johns, stj, red storm
boise|Boise State Broncos|boise state, boise, bsu, boise st
army|Army Black Knights|army
navy|Navy Midshipmen|navy, midshipmen
memp|Memphis Tigers|memphis, memp
tuln|Tulane Green Wave|tulane, tuln, green wave
unlv|UNLV Rebels|unlv
sdsu|San Diego State Aztecs|san diego state, sdsu, aztecs, san diego st
"""

EPL = """
ars|Arsenal|arsenal, ars, gunners
avl|Aston Villa|aston villa, villa, avl
bou|Bournemouth|bournemouth, afc bournemouth, bou
bre|Brentford|brentford, bre
bha|Brighton|brighton, brighton hove albion, bha, bri
bur|Burnley|burnley, bur
che|Chelsea|chelsea, che
cry|Crystal Palace|crystal palace, palace, cry
eve|Everton|everton, eve
ful|Fulham|fulham, ful
lee|Leeds United|leeds, leeds united, lee
liv|Liverpool|liverpool, liv
mci|Manchester City|manchester city, man city, mci
mun|Manchester United|manchester united, man united, man utd, mun
new|Newcastle United|newcastle, newcastle united, new
nfo|Nottingham Forest|nottingham forest, forest, nfo, not
sun|Sunderland|sunderland, sun
tot|Tottenham Hotspur|tottenham, spurs, tottenham hotspur, tot
whu|West Ham United|west ham, west ham united, whu
wol|Wolverhampton Wanderers|wolves, wolverhampton, wol
"""

# league code -> table text. "cfb" and "cbb" share the college table.
LEAGUE_TABLES = {
    "nfl": NFL, "nba": NBA, "mlb": MLB, "nhl": NHL,
    "cfb": COLLEGE, "cbb": COLLEGE, "epl": EPL,
}

# How each platform might spell a league -> our league code.
LEAGUE_ALIASES = {
    "nfl": "nfl", "football": "nfl",
    "nba": "nba", "basketball": "nba",
    "mlb": "mlb", "baseball": "mlb",
    "nhl": "nhl", "hockey": "nhl",
    "cfb": "cfb", "ncaaf": "cfb", "ncaa football": "cfb", "college football": "cfb", "ncaafb": "cfb",
    "cbb": "cbb", "ncaab": "cbb", "ncaa basketball": "cbb", "college basketball": "cbb",
    "ncaamb": "cbb", "mens college basketball": "cbb",
    "epl": "epl", "premier league": "epl", "english premier league": "epl",
}


def norm(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("'", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = text.replace(" and ", " and ")
    return re.sub(r"\s+", " ", text).strip()


def norm_league(name: str) -> Optional[str]:
    return LEAGUE_ALIASES.get(norm(name))


class TeamBook:
    """Looks up a canonical team id ("nfl:kc") from any alias."""

    def __init__(self) -> None:
        # league -> alias -> canonical code; None marks an ambiguous alias.
        self._by_league: dict[str, dict[str, Optional[str]]] = {}
        self.names: dict[str, str] = {}  # "nfl:kc" -> "Kansas City Chiefs"
        for league, table in LEAGUE_TABLES.items():
            for line in table.strip().splitlines():
                code, full, aliases = (p.strip() for p in line.split("|"))
                self.add(league, code, full, [a for a in aliases.split(",")])

    def add(self, league: str, code: str, full_name: str, aliases: list[str]) -> None:
        """Register a team and its aliases (safe to call repeatedly)."""
        league = league.lower()
        code = norm(code).replace(" ", "")
        table = self._by_league.setdefault(league, {})
        tid = f"{league}:{code}"
        self.names.setdefault(tid, full_name)
        for alias in [code, full_name, *aliases]:
            a = norm(alias)
            if not a:
                continue
            existing = table.get(a, "missing")
            if existing == "missing":
                table[a] = code
            elif existing is not None and existing != code:
                table[a] = None  # two teams claim it -> ambiguous, never use

    def lookup(self, league: str, name: str) -> Optional[str]:
        """Return "league:code" for a team name, or None if unknown/ambiguous."""
        table = self._by_league.get((league or "").lower())
        if not table:
            return None
        n = norm(name)
        variants = [n, re.sub(r"\bst\b", "state", n), re.sub(r"\bsaint\b", "st", n),
                    re.sub(r"\bstate\b", "st", n)]
        for v in variants:
            code = table.get(v)
            if code:
                return f"{league}:{code}"
        # "City Nickname" written in a form we don't list verbatim: accept it
        # only if BOTH halves point to the same team. ("Oregon State" is NOT
        # turned into "Oregon": "state" alone is not an alias of Oregon.)
        for v in variants:
            words = v.split()
            for i in range(1, len(words)):
                left, right = " ".join(words[:i]), " ".join(words[i:])
                lc, rc = table.get(left), table.get(right)
                if lc and lc == rc:
                    return f"{league}:{lc}"
        return None

    def display(self, team_id: str) -> str:
        return self.names.get(team_id, team_id.split(":", 1)[-1].upper())
