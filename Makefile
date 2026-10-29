TRIGGER := cd $(CURDIR)/trigger && npx wrangler
WORKFLOW := briefing.yml

.PHONY: help deploy tail secret run status logs

help:
	@echo "make deploy   Cloudflare 트리거 Worker 배포"
	@echo "make tail     Worker 실시간 로그"
	@echo "make secret   GITHUB_TOKEN 비밀값 교체"
	@echo "make run      GitHub workflow 수동 실행"
	@echo "make status   최근 실행 10건"
	@echo "make logs     마지막 실행 로그"

deploy:
	$(TRIGGER) deploy

tail:
	$(TRIGGER) tail

secret:
	$(TRIGGER) secret put GITHUB_TOKEN

run:
	cd $(CURDIR) && gh workflow run $(WORKFLOW)

status:
	cd $(CURDIR) && gh run list -L 10

logs:
	cd $(CURDIR) && gh run view $$(gh run list -L 1 --json databaseId -q '.[0].databaseId') --log
