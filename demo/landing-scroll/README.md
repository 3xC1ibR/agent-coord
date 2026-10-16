# Ribbon Field scroll concept

A standalone, buildless landing-page prototype. Native scrolling carries one
illustrative request through a lead agent, three specialists across Claude,
Codex, and Gemini, a direct message exchange, nested verification, and a final
human review. The example ships shared collections across a web app, mobile
app, and API. The mobile and API agents coordinate support for older app
versions; the mobile agent then recruits a verifier to check the flow across
devices. The final result pairs laptop and phone previews. It does not start
agents, submit forms, or deploy code. The early-access links open the existing
public site.

Run from the repository root:

```sh
python3 -m http.server 47831 --bind 127.0.0.1 --directory demo/landing-scroll
```

Open <http://127.0.0.1:47831/>. The files can also be opened directly from disk.
Google Fonts enhances the type; system serif/sans-serif fallbacks work offline.
There are no build dependencies or animation libraries.

Scroll position determines the scene; scrolling backward reverses it. The
browser keeps its normal scrolling behavior. Reduced-motion mode shows the
same six states without interpolated travel. A keyboard-accessible skip link
and the final replay link bypass or restart the sequence.

The user's reference was [How to turn your AI into a world-class designer](https://www.lennysnewsletter.com/p/how-to-turn-your-ai-into-a-world),
particularly the continuous transitions controlled by scrolling and the
emphasis on removing visual clutter. This implementation uses live text,
CSS transforms, and SVG paths so messages stay readable and the composition
can adapt to a phone. All results and messages are illustrative.
