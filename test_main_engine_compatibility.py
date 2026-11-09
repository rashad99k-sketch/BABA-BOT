"""Static regression checks for critical main-app integration contracts."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gpp_momentum_engine_call_matches_direction_neutral_signature():
    source = (ROOT / "ggpbot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = None
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MomentumFlowEngine":
            method = next((n for n in node.body if isinstance(n, ast.FunctionDef)
                           and n.name == "analyze_momentum_flow"), None)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "MomentumFlowEngine"
                    and node.func.attr == "analyze_momentum_flow"):
                calls.append(node)
    assert method is not None
    parameters = [arg.arg for arg in method.args.args]
    assert parameters == ["df"]
    assert calls, "Expected the GGP enrichment to use MomentumFlowEngine"
    assert all("side" not in {kw.arg for kw in call.keywords} for call in calls)


def test_web_dashboard_startup_uses_one_hosted_port_and_two_local_ports():
    source = (ROOT / "ggpbot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "start_dashboard_servers")
    function_source = ast.get_source_segment(source, function)
    assert 'hosted_port = os.getenv("PORT")' in function_source
    assert 'os.getenv("DASHBOARD_LOCAL_PORTS", "8000,8080")' in function_source
    assert 'bind_host = "0.0.0.0"' in function_source
    assert 'bind_host = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1")' in function_source
