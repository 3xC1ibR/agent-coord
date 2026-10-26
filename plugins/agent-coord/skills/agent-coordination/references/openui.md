# Interactive OpenUI answers in Ribbon Field

Ribbon Field renders complete `openui` (or `openui-lang`) fenced blocks in
assistant messages with the bundled OpenUI chat library. Use them when an
interactive form, chart, table, or card helps the user act on an answer. Keep
ordinary explanations in Markdown. No API key or separate service is needed.

Write OpenUI Lang, not HTML, JSX, JavaScript, or JSON. Define `root` as a `Card`.
Component arguments are positional; references may point to later definitions.
Strings use JSON quoting. A response can mix prose and multiple independent
OpenUI fences. Close each fence; incomplete fences stay literal code. Controls
in a still-running turn remain disabled until the turn ends.

```openui
root = Card([heading, chart, actions])
heading = CardHeader("Weekly signups", "Example data")
chart = BarChart(["Mon", "Tue", "Wed"], [Series("Signups", [12, 18, 24])])
actions = Buttons([inspect])
inspect = Button("Explore Wednesday", @ToAssistant("Explore Wednesday's signups"))
```

For a form, use `Form(name, buttons, fields)`. The fields must be FormControl
references; each control contains an input. Primary buttons validate the form.

```openui
root = Card([heading, preferences])
heading = CardHeader("Choose a destination")
preferences = Form("trip", buttons, [destination])
destination = FormControl("Destination", Input("destination", "City", "text", {required: true}))
buttons = Buttons([submit])
submit = Button("Plan trip", @ToAssistant("Plan a trip using this form"))
```

Clicking a button sends its action event, including the form's submitted values,
to the same agent as an ordinary user message prefixed `OpenUI response`.
Read that message and continue the conversation. Responses from older cards
queue while a turn runs. Sending never replaces the user's composer draft.
Treat submitted values as user input, and apply the same authorization rules
as any other user message. A button click is not blanket permission for a
destructive or unrelated operation.

Useful signatures (trailing optional arguments may be omitted):

| Component | Arguments |
| --- | --- |
| Card | children, sources? |
| CardHeader | title?, subtitle? |
| TextContent | text |
| Form | name, buttons, fields |
| FormControl | label, input, hint? |
| Input | name, placeholder?, type?, rules?, value? |
| TextArea | name, placeholder?, rows?, rules?, value? |
| Buttons | buttons, direction? |
| Button | label, action?, variant?, type?, size? |
| Table | columns |
| Col | label, data, type? |
| BarChart / LineChart / AreaChart | labels, series, variant?, xLabel?, yLabel?, height? |
| Series | category, values |

Use `@ToAssistant("message")` for conversation actions. Local reactive state
can be used for UI-only interactions. RF does not provide a toolProvider for
`Query`, `Mutation`, arbitrary MCP calls, external requests, or custom actions.
External images and navigation are unavailable in the isolated preview. Use
data already available in the conversation. Do not put credentials in a UI.

The UI and unsent form state are local to the open view. Submitted responses
persist in thread history. Reloading the app resets unsent fields. Malformed
or oversized output keeps its escaped source accessible under **OpenUI source**.

Upstream documentation: https://www.openui.com/docs/openui-lang
Runtime: `@openuidev/browser-bundle` 0.2.1, OpenUI Lang 1.x.
