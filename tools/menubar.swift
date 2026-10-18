// Menu bar item for claude-cache-keepalive: shows that keep-alives are on, what they saved today, and opens the dashboard.
// https://github.com/brunoqgalvao/claude-cache-keepalive
// Build: swiftc -O menubar.swift -o CacheKeepaliveBar
// Reads http://127.0.0.1:8797/api/badge (opencodex) and :8798 (Claude Code); set KEEPALIVE_BAR_PORTS to change.
import AppKit

struct Source { let name: String; let port: Int }

let sources: [Source] = {
    if let env = ProcessInfo.processInfo.environment["KEEPALIVE_BAR_PORTS"] {
        return env.split(separator: ",").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }.map { Source(name: $0 == 8798 ? "Claude Code" : "opencodex", port: $0) }
    }
    return [Source(name: "opencodex", port: 8797), Source(name: "Claude Code", port: 8798)]
}()

func tok(_ x: Double) -> String {
    let a = abs(x)
    if a >= 1e9 { return String(format: "%.2fB", x / 1e9) }
    if a >= 1e6 { return String(format: a >= 1e8 ? "%.0fM" : "%.1fM", x / 1e6) }
    if a >= 1e3 { return String(format: "%.0fk", x / 1e3) }
    return String(format: "%.0f", x)
}
func signed(_ x: Double) -> String { (x > 0.5 ? "+" : x < -0.5 ? "−" : "") + tok(abs(x)) }
func money(_ x: Double) -> String { (x < 0 ? "−" : "") + String(format: abs(x) >= 10 ? "US$ %.0f" : "US$ %.2f", abs(x)) }
func dur(_ s: Double) -> String {
    let s = max(0, s)
    if s < 60 { return "\(Int(s)) s" }
    if s < 3600 { return "\(Int((s / 60).rounded())) min" }
    let h = Int(s / 3600), m = Int((s.truncatingRemainder(dividingBy: 3600) / 60).rounded())
    return m > 0 && m < 60 ? "\(h) h \(String(format: "%02d", m))" : "\(h) h"
}
func fmt(_ t: Double, _ f: String) -> String { let d = DateFormatter(); d.dateFormat = f; return d.string(from: Date(timeIntervalSince1970: t)) }
func num(_ d: Any?, _ k: String) -> Double { ((d as? [String: Any])?[k] as? NSNumber)?.doubleValue ?? 0 }

final class Bar: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var badges: [Int: [String: Any]] = [:]
    var lastSave: [Int: Double] = [:]
    var flash: (text: String, until: Date)?
    var timer: Timer?
    var previewed = false

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            let img = NSImage(systemSymbolName: "bolt.fill", accessibilityDescription: "Prompt-cache keep-alive")
            img?.isTemplate = true
            b.image = img
            b.imagePosition = .imageLeading
            b.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            b.toolTip = "Prompt-cache keep-alive: net tokens saved today"
        }
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        render()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refresh() }
    }

    /// KEEPALIVE_BAR_PREVIEW=1 opens the menu once for a few seconds (for screenshots).
    func preview() {
        guard ProcessInfo.processInfo.environment["KEEPALIVE_BAR_PREVIEW"] == "1", let m = item.menu else { return }
        let close = Timer(timeInterval: 8, repeats: false) { _ in m.cancelTracking() }
        RunLoop.main.add(close, forMode: .common)
        item.button?.performClick(nil)
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    func refresh() {
        for s in sources {
            var req = URLRequest(url: URL(string: "http://127.0.0.1:\(s.port)/api/badge")!)
            req.timeoutInterval = 10
            URLSession.shared.dataTask(with: req) { data, _, _ in
                let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
                DispatchQueue.main.async { self.update(s.port, obj) }
            }.resume()
        }
    }

    func update(_ port: Int, _ obj: [String: Any]?) {
        badges[port] = obj
        if let ls = obj?["last_save"] as? [String: Any], let t = (ls["t"] as? NSNumber)?.doubleValue {
            // a return that just found its cache warm: show what it saved for a minute and a half
            if let prev = lastSave[port], t > prev, Date().timeIntervalSince1970 - t < 900 {
                flash = ("saved " + tok(num(ls, "saved")), Date().addingTimeInterval(90))
                Timer.scheduledTimer(withTimeInterval: 91, repeats: false) { [weak self] _ in self?.render() }
            }
            lastSave[port] = t
        }
        render()
        if !previewed && badges.count == sources.count { previewed = true; DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.preview() } }
    }

    func text(_ s: String, color: NSColor = .labelColor, bold: Bool = false, size: CGFloat = 13) -> NSAttributedString {
        NSAttributedString(string: s, attributes: [.foregroundColor: color, .font: bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.monospacedDigitSystemFont(ofSize: size, weight: .regular)])
    }

    func info(_ parts: [NSAttributedString]) -> NSMenuItem {
        let it = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        let s = NSMutableAttributedString()
        parts.forEach { s.append($0) }
        let p = NSMutableParagraphStyle()
        p.tabStops = [NSTextTab(textAlignment: .left, location: 96)]
        s.addAttribute(.paragraphStyle, value: p, range: NSRange(location: 0, length: s.length))
        // a plain label view: disabled menu items would grey the numbers out, enabled ones highlight on hover
        let label = NSTextField(labelWithAttributedString: s)
        label.sizeToFit()
        let v = NSView(frame: NSRect(x: 0, y: 0, width: label.frame.width + 34, height: label.frame.height + 6))
        label.frame.origin = NSPoint(x: 14, y: 3)
        v.addSubview(label)
        it.view = v
        return it
    }

    func row(_ label: String, _ value: String, _ tail: String = "") -> NSMenuItem {
        info([text(label + "\t", color: .secondaryLabelColor), text(value), text(tail.isEmpty ? "" : "  " + tail, color: .secondaryLabelColor)])
    }

    func render() {
        let live = sources.compactMap { s in badges[s.port].map { (s, $0) } }
        let anyOn = live.contains { ($0.1["on"] as? Bool) == true }
        let todayNet = live.reduce(0.0) { $0 + num($1.1["today"], "net") }
        if let f = flash, f.until < Date() { flash = nil }
        if let b = item.button {
            b.appearsDisabled = live.isEmpty || !anyOn
            b.title = live.isEmpty ? "" : !anyOn ? " off" : " " + (flash?.text ?? signed(todayNet))
        }
        guard let m = item.menu else { return }
        m.removeAllItems()
        if live.isEmpty {
            m.addItem(info([text("Prompt-cache keep-alive", bold: true)]))
            m.addItem(info([text("Dashboard not running on " + sources.map { "127.0.0.1:\($0.port)" }.joined(separator: " or "), color: .secondaryLabelColor)]))
        }
        for (i, (s, d)) in live.enumerated() {
            if i > 0 { m.addItem(.separator()) }
            let on = (d["on"] as? Bool) == true
            let now = num(d, "now")
            m.addItem(info([text("● ", color: on ? .systemGreen : .systemOrange, size: 11), text("Cache keep-alive · \(s.name) · " + (on ? "on" : "off"), bold: true)]))
            if !on, let r = d["disabled"] as? String { m.addItem(info([text(r, color: .secondaryLabelColor, size: 12)])) }
            let today = d["today"], d24 = d["d24"], all = d["all"]
            if !on && num(all, "pings") == 0 {   // switched off and never ran: nothing to report yet
                let open = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard(_:)), keyEquivalent: "")
                open.target = self
                open.tag = s.port
                m.addItem(open)
                continue
            }
            m.addItem(row("Today", signed(num(today, "net")) + " · " + money(num(today, "net_usd")), "\(Int(num(today, "rescues"))) warm returns · \(Int(num(today, "pings"))) keep-alives"))
            let ratio = num(d24, "cost") > 0 ? String(format: "%.1f× return on cost", num(d24, "saved") / num(d24, "cost")) : ""
            m.addItem(row("Last 24 h", signed(num(d24, "net")) + " · " + money(num(d24, "net_usd")), ratio))
            m.addItem(row("Since " + fmt(num(d, "since"), "d MMM"), signed(num(all, "net")) + " · " + money(num(all, "net_usd")), "\(Int(num(all, "rescues"))) warm returns"))
            var warm = "\(Int(num(d, "warm"))) chats · \(tok(num(d, "warm_tokens"))) cached"
            if let nk = (d["next_keepalive"] as? NSNumber)?.doubleValue { warm += " · next keep-alive in " + dur(nk - now) }
            m.addItem(row("Warm now", warm))
            if let ls = d["last_save"] as? [String: Any] {
                let title = (ls["title"] as? String).map { "“" + ($0.count > 30 ? String($0.prefix(29)) + "…" : $0) + "”" } ?? "a chat"
                m.addItem(row("Last save", "+" + tok(num(ls, "saved")) + " · " + title, fmt(num(ls, "t"), "HH:mm") + ", back after " + dur(num(ls, "away"))))
            }
            let open = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard(_:)), keyEquivalent: i == 0 ? "d" : "")
            open.target = self
            open.tag = s.port
            m.addItem(open)
        }
        m.addItem(.separator())
        let hide = NSMenuItem(title: "Hide until next login", action: #selector(hideBar), keyEquivalent: "")
        hide.target = self
        m.addItem(hide)
    }

    @objc func openDashboard(_ sender: NSMenuItem) { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(sender.tag)/")!) }
    @objc func hideBar() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let bar = Bar()
app.delegate = bar
app.setActivationPolicy(.accessory)
app.run()

