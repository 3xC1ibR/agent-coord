# Ribbon Field scroll concept

A standalone, buildless landing-page prototype. Native scrolling carries one
illustrative request through ten agents across Muse, Gemini, OpenAI, Claude,
and Moonshot. The example ships shared collections across a web app, mobile
app, and API. It does not start agents, submit forms, or deploy code. The
early-access links open the existing public site.

The seven scenes show a request reaching a Claude lead; OpenAI, Moonshot,
and Gemini team leads joining; those leads recruiting their own specialists;
a direct compatibility conversation; a fresh verifier joining; and a final
human review with laptop and phone previews. The web agent recruits Muse
for the interface and Claude for accessibility. The API agent recruits
Moonshot for data and OpenAI for permissions. The mobile agent recruits
Muse for sync, followed by OpenAI for verification.

The nested cards illustrate agents creating additional Ribbon Field agents,
not a claim that each provider's native subagent APIs behave identically.
OpenAI is represented through Codex, Moonshot through Kimi Code, and Muse
through Muse Code. Labels identify model families without promising a
particular version or account entitlement.

Run from the repository root:

```sh
python3 -m http.server 47831 --bind 127.0.0.1 --directory demo/landing-scroll
```

Open <http://127.0.0.1:47831/>. The files can also be opened directly from disk.
Google Fonts enhances the type; system serif/sans-serif fallbacks work offline.
There are no build dependencies or animation libraries.

The header and footer use the three-ribbon mark and wordmark styling from
[the live site](https://ribbonfield.com/). The favicon is a local copy of its
SVG, so the branding does not depend on a remote asset request.

Scroll position determines the scene; scrolling backward reverses it. The
browser keeps its normal scrolling behavior. Reduced-motion mode shows the
same seven states without interpolated travel. A keyboard-accessible skip link
and the final replay link bypass or restart the sequence.

The user's reference was [How to turn your AI into a world-class designer](https://www.lennysnewsletter.com/p/how-to-turn-your-ai-into-a-world),
particularly the continuous transitions controlled by scrolling and the
emphasis on removing visual clutter. This implementation uses live text,
CSS transforms, and SVG paths so messages stay readable and the composition
can adapt to a phone. All results and messages are illustrative.
