#!/bin/bash
# Fix: Remove secrets from git history and push clean repo

echo "🔒 Cleaning repository of secrets..."

# Remove .env.production from git tracking (keep local file)
git rm --cached .env.production 2>/dev/null || true

# Stage updated .gitignore
git add .gitignore .env.example

# Amend last commit to remove secrets
git commit --amend -m "Euro-English deployment - US number, secrets excluded"

# Force push to overwrite history (safe for new repo)
echo ""
echo "🚀 Force pushing clean repository..."
git push -u origin main --force-with-lease

echo ""
echo "✅ Repository cleaned and pushed!"
echo ""
echo "📋 Next: Add secrets to Railway directly (not in GitHub):"
echo "   https://railway.app"
echo ""
