UV := uv
DEMO_CONFIG := examples/demo/mkdocs.yml

.PHONY: test ty ci demo-build demo-serve

test:
	$(UV) run pytest -q

ty:
	$(UV) run ty check .

ci: ty test demo-build

demo-build:
	$(UV) run mkdocs build -f $(DEMO_CONFIG)

demo-serve:
	$(UV) run mkdocs serve -f $(DEMO_CONFIG)

