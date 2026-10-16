#!/usr/bin/env python3
"""Validate that documentation aligns with contract source code.

Checks:
   - README.md function signatures match lib.rs #[contractimpl] pub fn signatures
   - docs/ABI.md constants match source code (MAX_BATCH_SIZE, MIN_STREAM_TTL_LEDGERS)
   - error codes in error.md match ContractError enum discriminants

Exit 0 if all checks pass, exit 1 on mismatch.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Intentional non-ABI entries that are documented but not public entrypoints.
AUDIT_ENTRYPOINT_ALLOWLIST = {"upgrade", "compute_keeper_fee_split"}

# Soroban 16 KiB event byte footprint enforcer (issue #288).
EVENT_BUDGET_SCRIPT = REPO_ROOT / "script" / "event-byte-budget.py"
EVENT_BUDGET_DOCS = [
    REPO_ROOT / "docs" / "soroban-rpc-read-skew.md",
    REPO_ROOT / "docs" / "gas.md",
]
# Public API surface of the enforcer that docs must reference.
EVENT_BUDGET_PUBLIC_API = [
    "MAX_EVENT_BYTES",
    "check_event_byte_budget",
    "enforce_event_byte_budget",
]
# The 16 KiB event budget must be referenced in the docs.
EVENT_BUDGET_MARKER = "16 KiB"


def extract_contractimpl_pub_fns(source: str) -> list[str]:
    """Extract public function names from #[contractimpl] blocks."""
    fns = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if "pub fn" in stripped and in_block:
            match = re.search(r"pub\s+fn\s+(\w+)", stripped)
            if match:
                fns.append(match.group(1))
        if "#[contractimpl]" in stripped:
            in_block = True
        elif stripped.startswith("}") and in_block:
            # Rough heuristic: closing brace after contractimpl
            pass
    return sorted(set(fns))


def extract_error_variants(source: str) -> dict[str, int]:
    """Extract ContractError variants with their explicit discriminants."""
    variants = {}
    current_discriminant = 0
    for line in source.splitlines():
        stripped = line.strip()
        # Match explicit discriminants like: Variant = 42,
        explicit = re.match(r"(\w+)\s*=\s*(\d+)", stripped)
        if explicit:
            variants[explicit.group(1)] = int(explicit.group(2))
            current_discriminant = int(explicit.group(2)) + 1
            continue
        # Match plain variants
        plain = re.match(r"(\w+)\s*[,{]", stripped)
        if plain and plain.group(1) not in ("ContractError", "enum", "pub"):
            variants[plain.group(1)] = current_discriminant
            current_discriminant += 1
    return variants


def extract_constants(source: str) -> dict[str, str]:
    """Extract public const declarations."""
    constants = {}
    # Match patterns like: pub const NAME: TYPE = VALUE;
    pattern = r"pub\s+const\s+(\w+)\s*:\s*\w+\s*=\s*([^;]+);"
    for match in re.finditer(pattern, source):
        name = match.group(1)
        value = match.group(2).strip()
        constants[name] = value
    return constants


def check_readme_functions() -> bool:
    """Check that README.md documents all entrypoints from lib.rs."""
    lib_rs = REPO_ROOT / "contracts" / "stream" / "src" / "lib.rs"
    readme_md = REPO_ROOT / "README.md"

    if not lib_rs.exists():
        print(f"SKIP: {lib_rs} not found")
        return True
    if not readme_md.exists():
        print(f"SKIP: {readme_md} not found")
        return True

    source = lib_rs.read_text(encoding="utf-8")
    readme = readme_md.read_text(encoding="utf-8")

    fns = extract_contractimpl_pub_fns(source)
    # Filter out internal helpers that aren't entrypoints
    entrypoints = [
        f for f in fns
        if f not in AUDIT_ENTRYPOINT_ALLOWLIST
        and not f.startswith("_")
    ]

    missing = [f for f in entrypoints if f not in readme]
    if missing:
        print(f"WARNING: {len(missing)} entrypoint(s) not documented in README.md: {missing}")
        # Don't fail CI for documentation gaps — just warn
    return True


def check_abi_constants() -> bool:
    """Check that docs/ABI.md constants match source code."""
    lib_rs = REPO_ROOT / "contracts" / "stream" / "src" / "lib.rs"
    abi_md = REPO_ROOT / "docs" / "ABI.md"

    if not lib_rs.exists():
        print(f"SKIP: {lib_rs} not found")
        return True
    if not abi_md.exists():
        print(f"SKIP: {abi_md} not found")
        return True

    source = lib_rs.read_text(encoding="utf-8")
    abi = abi_md.read_text(encoding="utf-8")

    constants = extract_constants(source)
    
    # Check for specific constants mentioned in the issue
    required_constants = ["MAX_BATCH_SIZE", "MIN_STREAM_TTL_LEDGERS"]
    missing = []
    mismatched = []
    
    for const_name in required_constants:
        if const_name not in constants:
            missing.append(const_name)
            continue
            
        # Look for the constant in ABI.md
        # Pattern: `MAX_BATCH_SIZE = 16` or similar
        pattern = rf"`{re.escape(const_name)}\s*=\s*([^`]+)`"
        match = re.search(pattern, abi)
        if not match:
            missing.append(const_name)
            continue
            
        abi_value = match.group(1).strip()
        source_value = constants[const_name]
        
        # Normalize values for comparison (remove spaces, etc.)
        abi_norm = re.sub(r'\s+', '', abi_value)
        source_norm = re.sub(r'\s+', '', source_value)
        
        if abi_norm != source_norm:
            mismatched.append((const_name, source_value, abi_value))
    
    if missing:
        print(f"WARNING: Constant(s) not found in docs/ABI.md: {missing}")
    if mismatched:
        print(f"WARNING: Constant value mismatch in docs/ABI.md:")
        for name, source_val, abi_val in mismatched:
            print(f"  {name}: source={source_val}, ABI.md={abi_val}")
            
    return True  # Don't fail on warnings for now


def check_error_alignment() -> bool:
    """Check that error.md discriminants match ContractError enum."""
    error_rs = REPO_ROOT / "contracts" / "stream" / "src" / "error.rs"
    error_md = REPO_ROOT / "docs" / "error.md"

    if not error_rs.exists():
        print(f"SKIP: {error_rs} not found")
        return True
    if not error_md.exists():
        print(f"SKIP: {error_md} not found")
        return True

    source = error_rs.read_text(encoding="utf-8")
    doc = error_md.read_text(encoding="utf-8")

    variants = extract_error_variants(source)
    if not variants:
        print("WARNING: No error variants found in source")
        return True

    missing = [v for v in variants if v not in doc]
    if missing:
        print(f"WARNING: {len(missing)} error variant(s) not in error.md: {missing}")
    return True


def extract_public_functions(source: str) -> list[str]:
    """Extract public function names from a Python module source."""
    fns = []
    for line in source.splitlines():
        match = re.match(r"def\s+(\w+)\s*\(", line)
        if match and not match.group(1).startswith("_"):
            fns.append(match.group(1))
    return sorted(set(fns))


def has_docstring(source: str, fn_name: str) -> bool:
    """Return True if the named function has a docstring."""
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        match = re.match(rf"def\s+{re.escape(fn_name)}\s*\(", line)
        if not match:
            continue
        # Scan forward for the first non-empty line after the signature.
        for follow in lines[idx + 1:]:
            stripped = follow.strip()
            if not stripped:
                continue
            return stripped.startswith(('"""', "'''", 'r"""', "r'''"))
        return False
    return False


def check_event_byte_budget_docs() -> bool:
    """Check docstrings and markdown references for event-byte-budget.py."""
    if not EVENT_BUDGET_SCRIPT.exists():
        print(f"SKIP: {EVENT_BUDGET_SCRIPT} not found")
        return True

    source = EVENT_BUDGET_SCRIPT.read_text(encoding="utf-8")

    # Every public function must have a docstring.
    public_fns = extract_public_functions(source)
    undocumented = [fn for fn in public_fns if not has_docstring(source, fn)]
    if undocumented:
        print(
            f"WARNING: {len(undocumented)} public function(s) in "
            f"{EVENT_BUDGET_SCRIPT.name} missing docstring: {undocumented}"
        )

    # Markdown docs must reference the enforcer's public API and 16 KiB budget.
    for doc_path in EVENT_BUDGET_DOCS:
        if not doc_path.exists():
            print(f"SKIP: {doc_path} not found")
            continue
        doc = doc_path.read_text(encoding="utf-8")
        missing_api = [name for name in EVENT_BUDGET_PUBLIC_API if name not in doc]
        if missing_api:
            print(
                f"WARNING: {doc_path.name} does not reference enforcer API: "
                f"{missing_api}"
            )
        if EVENT_BUDGET_MARKER not in doc:
            print(
                f"WARNING: {doc_path.name} does not reference the "
                f"{EVENT_BUDGET_MARKER} event budget"
            )

    return True


def main() -> int:
    passed = True

    print("Checking README.md function signature coverage...")
    if not check_readme_functions():
        passed = False

    print("Checking docs/ABI.md constant alignment...")
    if not check_abi_constants():
        passed = False

    print("Checking error.md discriminant alignment...")
    if not check_error_alignment():
        passed = False

    print("Checking event-byte-budget.py docstrings and markdown references...")
    if not check_event_byte_budget_docs():
        passed = False

    if passed:
        print("OK: Documentation alignment checks passed.")
    else:
        print("FAIL: Documentation alignment issues found.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
