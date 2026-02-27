#!/bin/bash
# Deploy Script - AI Recruitment Caller
# Usage: ./deploy.sh

set -e

echo "🚀 AI Recruitment Caller - Euro-English Deployment"
echo "=================================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

if ! command -v git &> /dev/null; then
    echo -e "${RED}❌ Git not installed${NC}"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 not installed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Prerequisites OK${NC}"
echo ""

# Copy production env
echo -e "${YELLOW}Setting up production environment...${NC}"
cp .env.production .env
echo -e "${GREEN}✓ Environment configured for UK (+44) deployment${NC}"
echo ""

# Initialize git if needed
if [ ! -d ".git" ]; then
    echo -e "${YELLOW}Initializing Git repository...${NC}"
    git init
    git add .
    git commit -m "Initial commit - Euro-English deployment"
    echo -e "${GREEN}✓ Git repository initialized${NC}"
else
    echo -e "${YELLOW}Git repository exists, committing changes...${NC}"
    git add .
    git commit -m "Update: Euro-English deployment config" || true
    echo -e "${GREEN}✓ Changes committed${NC}"
fi
echo ""

# Instructions for Railway
echo -e "${GREEN}✅ Project Ready for Deployment${NC}"
echo ""
echo "=================================================="
echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo "1. Create GitHub repository:"
echo "   https://github.com/new"
echo ""
echo "2. Push code to GitHub:"
echo "   git remote add origin https://github.com/YOUR_USERNAME/ai-recruitment-caller.git"
echo "   git push -u origin main"
echo ""
echo "3. Deploy to Railway:"
echo "   https://railway.app/new"
echo "   - Select 'Deploy from GitHub repo'"
echo "   - Choose your repository"
echo "   - Railway will auto-detect railway.toml"
echo ""
echo "4. Add environment variables in Railway dashboard:"
echo "   VAPI_API_KEY=c44f5a54-a0fd-4a3c-b358-c7978a31f591"
echo "   VAPI_PHONE_NUMBER_ID=1d7a2f79-f76a-4924-a5ce-e61fbb416116"
echo "   OPENAI_API_KEY=sk-proj-..."
echo "   (Copy all from .env.production)"
echo ""
echo "5. Railway will provide a public URL like:"
echo "   https://ai-recruitment-caller.up.railway.app"
echo ""
echo "6. Update webhook URL in your .env:"
echo "   Edit .env.production and set:"
echo "   WEBHOOK_BASE_URL=https://your-railway-url.up.railway.app"
echo ""
echo "✅ Your UK number (+44) is ready for Euro-English calls!"
echo ""
echo "Test locally first:"
echo "   python -m app.cli server"
echo ""
