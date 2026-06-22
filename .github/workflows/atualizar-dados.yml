name: Atualizar dados Metabase
on:
  schedule:
    - cron: '0 10 * * *'   # 07:00 BRT
  workflow_dispatch:

jobs:
  atualizar:
    name: Buscar dados e atualizar dashboard
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Checkout
        uses: actions/checkout@v4.2.2

      - name: Setup Python 3.11
        uses: actions/setup-python@v5.6.0
        with:
          python-version: '3.11'

      - name: Instalar dependências
        run: pip install requests

      - name: Atualizar dados Metabase
        env:
          MB_URL:  ${{ vars.MB_URL || 'https://metabase.gocase.com.br' }}
          MB_DB:   ${{ vars.MB_DB  || '3' }}
          MB_USER: ${{ secrets.MB_USER }}
          MB_PASS: ${{ secrets.MB_PASS }}
        run: python scripts/atualizar_dados.py

      - name: Commitar index.html atualizado
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add index.html
          [ -f an_cache.js ] && git add an_cache.js || true
          if git diff --staged --quiet; then
            echo "Sem mudanças."
          else
            git commit -m "chore: atualizar dados — $(date +'%d/%m/%Y %H:%M')"
            git pull --rebase origin main
            git push
            echo "✅ Publicado no GitHub"
          fi

      - name: Invalidar cache GoDeploy
        env:
          DEPLOY_KEY: ${{ secrets.DEPLOY_KEY }}
        run: |
          curl -s -X POST \
            -H "Authorization: Bearer $DEPLOY_KEY" \
            https://requisicao-lojas.devgogroup.com/refresh
          echo "✅ GoDeploy atualizado"
