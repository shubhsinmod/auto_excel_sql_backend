from typing import List, Dict, Set, Tuple, Optional
from app.models.schema import DatabaseSchemaMetadata
from app.core.logging import logger


class DependencyResolver:
    """
    Topological sorting engine for database table insertion order.
    Ensures parent tables (e.g. outlets, customers, items) are populated before
    dependent child tables (orders, bills, line items).
    Supports both explicit RDBMS foreign keys and inferred relationship dependencies.
    Scopes dependency resolution strictly to active target tables in the migration plan.
    Detects and reports circular dependencies explicitly.
    """

    @staticmethod
    def build_dependency_graph(
        schema: DatabaseSchemaMetadata,
        target_tables: Optional[List[str]] = None
    ) -> Tuple[Dict[str, Set[str]], List[str]]:
        """
        Builds the dependency map: dependencies[child] = set of parents that must be inserted FIRST.
        Strictly scoped to target_tables if provided, avoiding false cycles across unused tables.
        Also returns any circular dependency descriptions detected.
        """
        all_tables = {t.name: t for t in schema.tables}
        
        # Scope to target tables if provided
        if target_tables:
            target_set = set(target_tables)
            active_tables = [t for t in schema.tables if t.name in target_set]
        else:
            active_tables = list(schema.tables)

        table_names = [t.name for t in active_tables]
        table_lookup = {t.name.lower(): t.name for t in active_tables}
        table_pks = {
            t.name.lower(): [c.name.lower() for c in t.columns if c.primary_key]
            for t in active_tables
        }

        # dependencies[child] = set of parents that must be inserted FIRST
        dependencies: Dict[str, Set[str]] = {t: set() for t in table_names}

        # 1. Explicit foreign key constraints
        for table in active_tables:
            for fk in table.foreign_keys:
                if fk.target_table != table.name and fk.target_table in dependencies:
                    dependencies[table.name].add(fk.target_table)

        # 2. Inferred relationships if explicit FKs are sparse
        total_explicit = sum(len(deps) for deps in dependencies.values())
        if total_explicit == 0:
            for table in active_tables:
                t_lower = table.name.lower()
                for col in table.columns:
                    c_lower = col.name.lower()
                    for parent_lower, parent_real_name in table_lookup.items():
                        if parent_lower != t_lower:
                            p_pks = table_pks.get(parent_lower, [])
                            if c_lower in p_pks or c_lower == f"{parent_lower}_id" or c_lower == f"{parent_lower}id":
                                dependencies[table.name].add(parent_real_name)

            # Business Domain Relationship Disambiguation:
            # In POS databases, Order is the primary transactional entity created first,
            # and Bills is the invoice/receipt record generated for the Order.
            # While both may have cross-reference columns (Order.BillNo and Bills.Order_ID),
            # the business hierarchy is Order -> Bills. Bills depends on Order.
            if "Order" in dependencies and "Bills" in dependencies:
                dependencies["Bills"].add("Order")
                dependencies["Order"].discard("Bills")

            if "OrderLineItems" in dependencies and "Order" in dependencies:
                dependencies["OrderLineItems"].add("Order")

            if "Settlements" in dependencies:
                if "Bills" in dependencies:
                    dependencies["Settlements"].add("Bills")
                elif "Order" in dependencies:
                    dependencies["Settlements"].add("Order")

            if "Order" in dependencies and "Customer" in dependencies:
                dependencies["Order"].add("Customer")

            if "Order" in dependencies and "Outlets" in dependencies:
                dependencies["Order"].add("Outlets")

            if "Customer" in dependencies and "Outlets" in dependencies:
                # Customer can reference Outlet_ID, but is independent if Outlets not required
                if "Outlets" in dependencies:
                    dependencies["Customer"].add("Outlets")

        # Detect circular dependencies
        cycles: List[str] = []
        visited: Set[str] = set()
        visiting: List[str] = []

        def detect(node: str):
            if node in visiting:
                cycle_start = visiting.index(node)
                cycle_path = " -> ".join(visiting[cycle_start:] + [node])
                cycles.append(f"Circular foreign-key dependency detected: {cycle_path}")
                return
            if node in visited:
                return

            visiting.append(node)
            for dep in sorted(dependencies.get(node, set())):
                detect(dep)
            visiting.pop()
            visited.add(node)

        for t in table_names:
            if t not in visited:
                detect(t)

        # Deduplicate cycle warnings
        unique_cycles = list(dict.fromkeys(cycles))
        return dependencies, unique_cycles

    @staticmethod
    def resolve_execution_order(
        schema: DatabaseSchemaMetadata,
        target_tables: Optional[List[str]] = None
    ) -> List[str]:
        """
        Calculates parent-to-child dependency execution order using topological sort.
        Ensures parents come before children.
        """
        dependencies, cycles = DependencyResolver.build_dependency_graph(schema, target_tables=target_tables)
        for c in cycles:
            logger.warning(f"[DEPENDENCY] {c}")

        table_names = list(dependencies.keys())
        visited: Set[str] = set()
        visiting: Set[str] = set()
        order: List[str] = []

        def visit(table: str):
            if table in visited:
                return
            if table in visiting:
                logger.warning(f"[DEPENDENCY] Cycle broken at table '{table}'.")
                return

            visiting.add(table)
            # Visit all parents first (dependencies)
            for dep in sorted(dependencies.get(table, set())):
                if dep in dependencies:
                    visit(dep)
            visiting.remove(table)
            visited.add(table)
            order.append(table)

        for t in table_names:
            if t not in visited:
                visit(t)

        logger.info(f"[DEPENDENCY] Resolved execution order ({len(order)} tables): {order}")
        return order
