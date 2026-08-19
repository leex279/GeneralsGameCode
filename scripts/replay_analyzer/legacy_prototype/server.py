# Copyright 2026 TheSuperHackers
#
# Live Web Dashboard & Local Server for Zero Hour Replay Intelligence.

import http.server
import socketserver
import json
import cgi
import io
import os
import sys

# Ensure root in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.replay_analyzer.parser import ReplayParser
from scripts.replay_analyzer.metrics import MetricsCalculator
from scripts.replay_analyzer.heuristics import StrategyAnalyzer
from scripts.replay_analyzer.spatial import SpatialAnalyzer
from scripts.replay_analyzer.commentary import CasterCommentaryGenerator
from scripts.replay_analyzer.html_generator import HTMLReportGenerator

PORT = 8080

class ReplayServerHandler(http.server.SimpleHTTPRequestHandler):

    def do_POST(self):
        if self.path == "/api/analyze":
            try:
                ctype, pdict = cgi.parse_header(self.headers.get("content-type"))
                if ctype == "multipart/form-data":
                    pdict["boundary"] = bytes(pdict["boundary"], "utf-8")
                    fields = cgi.parse_multipart(self.rfile, pdict)
                    file_bytes = fields.get("replay_file", [b""])[0]

                    if not file_bytes:
                        self.send_error(400, "No replay file provided.")
                        return

                    # Save to temp file
                    temp_path = os.path.join(os.path.dirname(__file__), "_temp_uploaded.rep")
                    with open(temp_path, "wb") as f:
                        f.write(file_bytes)

                    parser = ReplayParser(temp_path)
                    parsed = parser.parse(parse_commands=True)
                    calculator = MetricsCalculator(parsed)
                    metrics = calculator.calculate()
                    spatial = SpatialAnalyzer(parsed).analyze()

                    player_reports = {}
                    for pid, p in metrics.players.items():
                        player_reports[p.player_name] = StrategyAnalyzer.evaluate_player(p, metrics, spatial)

                    scorecard = StrategyAnalyzer.analyze_match_quality(metrics, player_reports, spatial)
                    commentary = CasterCommentaryGenerator(parsed, metrics, player_reports, scorecard, spatial).generate()

                    html_gen = HTMLReportGenerator(parsed, metrics, player_reports, scorecard, spatial, commentary)
                    html_content = html_gen.generate()

                    if os.path.exists(temp_path):
                        os.remove(temp_path)

                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_content.encode("utf-8"))
                    return

            except Exception as e:
                self.send_error(500, f"Error parsing replay: {str(e)}")
                import traceback
                traceback.print_exc()
                return

        self.send_error(404, "Endpoint not found.")

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            # Serve Replay 1 report or default upload landing page
            report_path = os.path.join(os.path.dirname(__file__), "../../replay1_report.html")
            if os.path.exists(report_path):
                with open(report_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
                return

        super().do_GET()


def run_server(port: int = PORT):
    with socketserver.TCPServer(("", port), ReplayServerHandler) as httpd:
        print(f"================================================================================")
        print(f"  C&C GENERALS: ZERO HOUR - REPLAY INTELLIGENCE DASHBOARD")
        print(f"  Live Server running at: http://localhost:{port}")
        print(f"  Open in your browser to view reports & drag-and-drop replays!")
        print(f"================================================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    run_server()
