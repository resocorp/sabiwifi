# SabiWiFi

**Turn Your Internet Into a Business** — Powered by PHSWEB LTD

SabiWiFi is a WiFi reselling platform for the Nigerian market. Resellers plug in a pre-configured MikroTik router, set their prices, and earn money as subscribers connect and pay.

## Tech Stack

- **Backend:** Django 5.1, Django REST Framework
- **Database:** PostgreSQL (shared with FreeRADIUS via rlm_sql)
- **Frontend:** Server-rendered Django templates, TailwindCSS CDN (dashboard), hand-written CSS (captive portal)
- **Payments:** Paystack with automatic split payments
- **SMS:** Termii
- **RADIUS:** FreeRADIUS + pyrad (CoA)
- **Router Management:** RouterOS API over WireGuard tunnels

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+

### Setup

```bash
# Clone and enter the project
cd sabiwifi

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Copy environment config
copy .env.example .env  # Windows
# cp .env.example .env  # Linux/Mac

# Edit .env with your PostgreSQL credentials

# Create database
# psql -U postgres -c "CREATE DATABASE sabiwifi;"
# psql -U postgres -c "CREATE USER sabiwifi WITH PASSWORD 'your-password';"
# psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE sabiwifi TO sabiwifi;"

# Run migrations
python manage.py migrate

# Create superuser (for Django admin)
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

### URLs

| URL | Purpose |
|---|---|
| `http://localhost:8000/` | Public landing page |
| `http://localhost:8000/signup/` | Reseller signup |
| `http://localhost:8000/login/` | Reseller login |
| `http://localhost:8000/dashboard/` | Reseller dashboard |
| `http://localhost:8000/admin/` | Django admin (operator) |
| `http://localhost:8000/operator/overview/` | Operator overview dashboard |
| `http://localhost:8000/portal/?r={serial}` | Captive portal |
| `http://localhost:8000/account/` | Subscriber self-service |

### API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/resellers/signup/` | POST | Register reseller |
| `/api/resellers/login/` | POST | Reseller login |
| `/api/resellers/dashboard/` | GET | Dashboard metrics |
| `/api/resellers/plans/` | GET/POST | List/create plans |
| `/api/resellers/plans/{id}/` | GET/PUT | Plan detail/update |
| `/api/routers/add/` | POST | Claim router by serial |
| `/api/routers/provision/{serial}/` | GET | Router phone-home (returns .rsc) |
| `/api/portal/plans/?reseller={slug}` | GET | Available plans |
| `/api/billing/webhook/` | POST | Paystack webhook |

## Project Structure

```
sabiwifi/
├── config/          # Django settings (base/dev/prod), URLs, WSGI
├── radius/          # FreeRADIUS unmanaged models, CoA sender, RADIUS sync
├── accounts/        # Reseller + Subscriber models, auth, serializers
├── plans/           # ServicePlan, Subscription, RADIUS group sync
├── billing/         # Payment model, Paystack provider abstraction
├── routers/         # Router model, provisioning API, .rsc generation
├── portal/          # Captive portal + subscriber self-service
├── dashboard/       # Reseller dashboard (server-rendered)
├── operator_panel/  # PlatformSettings, Django admin, operator overview
├── notifications/   # SMS alerts (Termii) — Phase 3
├── templates/       # All Django templates
├── static/          # Static files including MikroTik hotspot redirects
└── manage.py
```

## Development Phases

- **Phase 1 (current):** Foundation — models, reseller auth, router provisioning, plans CRUD, dashboard
- **Phase 2:** Subscriber & Billing — captive portal, Paystack payments, plan activation, expiry
- **Phase 3:** Operations — SMS notifications, branding editor, router health monitoring
