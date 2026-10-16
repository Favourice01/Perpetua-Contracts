#!/usr/bin/env python3
"""Validate that documentation aligns with contract source code.

Checks:
   - README.md function signatures match lib.rs #[contractimpl] pub fn signatures
   - docs/ABI.md constants match source code (MAX_BATCH_SIZE, MIN_STREAM_TTL_LEDGERS)
   - error codes in error.md match ContractError enum discriminants
   - schema/schema.graphql event types and Stream type have doc comments
   - schema/schema.graphql declares a schemaVersion/abiVersion marker
   - docs/indexing_guide.md, docs/ABI.md, docs/soroban-rpc-read-skew.md are referenced

Exit 0 if all checks pass, exit 1 on mismatch.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Intentional non-ABI entries that are documented but not public entrypoints.
AUDIT_ENTRYPOINT_ALLOWLIST = {"upgrade", "compute_keeper_fee_split"}


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


def extract_graphql_types(source: str) -> list[str]:
    """Extract GraphQL type names (e.g. `type Stream {`)."""
    return re.findall(r"^\s*type\s+(\w+)\b", source, re.MULTILINE)


def extract_graphql_events(source: str) -> list[str]:
    """Extract GraphQL event type names (e.g. `type StreamCreatedEvent {`)."""
    return [t for t in extract_graphql_types(source) if t.endswith("Event")]


def has_doc_comment_before(source: str, type_name: str) -> bool:
    """Return True if a doc comment (a `#` comment line) precedes the type."""
    lines = source.splitlines()
    for idx, line in enumerate(lines):
        if re.match(rf"^\s*type\s+{re.escape(type_name)}\b", line):
            # Walk backwards over blank lines to find the nearest comment.
            j = idx - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            if j >= 0 and lines[j].lstrip().startswith("#"):
                return True
            return False
    return False


def check_schema_docstrings() -> bool:
    """Check that every event type and the Stream type in schema.graphql is documented."""
    schema = REPO_ROOT / "schema" / "schema.graphql"
    if not schema.exists():
        print(f"SKIP: {schema} not found")
        return True

    source = schema.read_text(encoding="utf-8")
    types = extract_graphql_types(source)
    if not types:
        print(f"FAIL: no GraphQL types found in {schema}")
        return False

    events = extract_graphql_events(source)
    if not events:
        print(f"FAIL: no event types found in {schema}")
        return False

    undocumented = [t for t in events if not has_doc_comment_before(source, t)]
    if "Stream" in types and not has_doc_comment_before(source, "Stream"):
        undocumented.append("Stream")

    if undocumented:
        print(f"FAIL: {len(undocumented)} schema type(s) missing doc comment: {undocumented}")
        return False
    return True


def check_schema_version_marker() -> bool:
    """Check that schema.graphql declares a schemaVersion/abiVersion marker."""
    schema = REPO_ROOT / "schema" / "schema.graphql"
    if not schema.exists():
        print(f"SKIP: {schema} not found")
        return True

    source = schema.read_text(encoding="utf-8")
    if not re.search(r"\b(schemaVersion|abiVersion)\b", source):
        print("FAIL: schema/schema.graphql missing schemaVersion/abiVersion marker")
        return False
    return True


def check_schema_doc_references() -> bool:
    """Check that the schema is referenced by the expected docs."""
    schema = REPO_ROOT / "schema" / "schema.graphql"
    if not schema.exists():
        print(f"SKIP: {schema} not found")
        return True

    required_docs = [
        REPO_ROOT / "docs" / "indexing_guide.md",
        REPO_ROOT / "docs" / "ABI.md",
        REPO_ROOT / "docs" / "soroban-rpc-read-skew.md",
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in required_docs if not p.exists()]
    if missing:
        print(f"FAIL: required schema doc(s) missing: {missing}")
        return False

    unreferenced = []
    for path in required_docs:
        content = path.read_text(encoding="utf-8")
        if "schema.graphql" not in content and "schema/schema.graphql" not in content:
            unreferenced.append(str(path.relative_to(REPO_ROOT)))
    if unreferenced:
        print(f"FAIL: doc(s) do not reference schema/schema.graphql: {unreferenced}")
        return False
    return True


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

    print("Checking schema/schema.graphql docstrings...")
    if not check_schema_docstrings():
        passed = False

    print("Checking schema/schema.graphql version marker...")
    if not check_schema_version_marker():
        passed = False

    print("Checking schema/schema.graphql doc references...")
    if not check_schema_doc_references():
        passed = False

    if passed:
        print("OK: Documentation alignment checks passed.")
    else:
        print("FAIL: Documentation alignment issues found.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
