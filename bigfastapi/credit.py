import datetime as _dt
import time
from uuid import uuid4

import fastapi
import requests
import sqlalchemy.orm as _orm
from decouple import config
from fastapi import APIRouter
from fastapi_pagination import Page, paginate, add_pagination
from starlette import status
from starlette.responses import RedirectResponse

from bigfastapi.db.database import get_db
from .auth_api import is_authenticated
from .models import credit_wallet_models as model, organisation_models, credit_wallet_conversion_models, wallet_models, \
    wallet_transaction_models
from .schemas import credit_wallet_schemas as schema, credit_wallet_conversion_schemas
from .schemas import users_schemas
from .utils.utils import generate_payment_link

app = APIRouter(tags=["CreditWallet"], )


@app.post("/credits/rates", response_model=credit_wallet_conversion_schemas.CreditWalletConversion)
async def add_rate(
        body: credit_wallet_conversion_schemas.CreditWalletConversion,
        user: users_schemas.User = fastapi.Depends(is_authenticated),
        db: _orm.Session = fastapi.Depends(get_db),
):
    conversion = await _get_credit_wallet_conversion(currency=body.currency_code, db=db)
    if conversion is not None:
        raise fastapi.HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Currency " + body.currency_code + " already has a conversion rate")

    rate = credit_wallet_conversion_models.CreditWalletConversion(id=uuid4().hex,
                                                                  credit_wallet_type=body.credit_wallet_type,
                                                                  rate=body.rate,
                                                                  currency_code=body.currency_code)

    db.add(rate)
    db.commit()
    db.refresh(rate)

    return rate


@app.get("/credits/rates", response_model=Page[credit_wallet_conversion_schemas.CreditWalletConversion])
async def get_rates(
        user: users_schemas.User = fastapi.Depends(is_authenticated),
        db: _orm.Session = fastapi.Depends(get_db),
):
    rates = db.query(credit_wallet_conversion_models.CreditWalletConversion)
    return paginate(list(rates))


@app.get("/credits/rates/{currency_code}", response_model=credit_wallet_conversion_schemas.CreditWalletConversion)
async def get_rate(
        currency_code: str,
        user: users_schemas.User = fastapi.Depends(is_authenticated),
        db: _orm.Session = fastapi.Depends(get_db),
):
    rate = db.query(credit_wallet_conversion_models.CreditWalletConversion).filter_by(
        currency_code=currency_code).first()
    if rate is None:
        raise fastapi.HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Currency " + currency_code + " does not have a conversion rate")

    return rate


@app.get("/credits/callback")
async def verify_payment_transaction(
        status: str,
        tx_ref: str,
        transaction_id='',
        db: _orm.Session = fastapi.Depends(get_db),
):
    frontendUrl = config("FRONTEND_URL")
    if status == 'successful':
        flutterwaveKey = config('FLUTTERWAVE_SEC_KEY')
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + flutterwaveKey}
        url = 'https://api.flutterwave.com/v3/transactions/' + transaction_id + '/verify'
        verificationRequest = requests.get(url, headers=headers)
        rootUrl = config('API_URL')
        retryLink = rootUrl + '/credits/callback?status=' + status + '&tx_ref=' + tx_ref
        retryLink += '' if transaction_id == '' else ('&transaction_id=' + transaction_id)
        if verificationRequest.status_code == 200:
            jsonResponse = verificationRequest.json()
            ref = jsonResponse['data']['tx_ref']
            frontendUrl = jsonResponse['data']['meta']['redirect_url']
            if jsonResponse['status'] == 'success' and tx_ref == ref:
                if jsonResponse['data']['status'] == 'successful':
                    user_id, organization_id, _ = tx_ref.split('-')
                    amount = jsonResponse['data']['amount']
                    currency = jsonResponse['data']['currency']
                    wallet = await _get_wallet(organization_id=organization_id, currency=currency, db=db)

                    wallet_transaction = db.query(wallet_transaction_models.WalletTransaction).filter_by(
                        transaction_ref=tx_ref).filter_by(wallet_id=wallet.id).first()
                    if wallet_transaction is not None:
                        response = RedirectResponse(
                            url=frontendUrl + '?status=error&message=Transaction already processed')
                        return response
                    try:
                        await _update_wallet(wallet=wallet, amount=amount, db=db, currency=currency, tx_ref=ref)

                        conversion = await _get_credit_wallet_conversion(currency=currency, db=db)
                        credits_to_add = amount * conversion.rate
                        await _update_wallet(wallet=wallet, amount=-amount, db=db, currency=currency,
                                             tx_ref=str(credits_to_add) + ' credit refill')

                        credit = db.query(model.CreditWallet).filter_by(organization_id=organization_id).first()

                        credit.amount += credits_to_add
                        credit.last_updated = _dt.datetime.utcnow()
                        db.commit()
                        db.refresh(credit)

                        response = RedirectResponse(url=frontendUrl + '?status=success&message=Credit refilled')
                        return response
                    except fastapi.HTTPException:
                        response = RedirectResponse(
                            url=frontendUrl + '?status=error&message=An error occurred while refilling your credit. '
                                              'Please try again&link=' + retryLink)
                        return response

                else:
                    response = RedirectResponse(url=frontendUrl + '?status=error&message=Transaction not found')
                    return response

        response = RedirectResponse(
            url=frontendUrl + '?status=error&message=An error occurred. Please try again&link=' + retryLink)
        return response

    else:
        response = RedirectResponse(url=frontendUrl + '?status=error&message=Payment was not successful')
        return response


@app.get("/credits/{organization_id}", response_model=schema.CreditWalletResponse)
async def get_credit(
        organization_id: str,
        user: users_schemas.User = fastapi.Depends(is_authenticated),
        db: _orm.Session = fastapi.Depends(get_db),
):
    """Gets the credit of an organization"""
    return await _get_credit(organization_id=organization_id, user=user, db=db)


@app.post("/credits/{organization_id}", response_model=schema.CreditWalletFundResponse)
async def add_credit(body: schema.CreditWalletFund,
                     organization_id: str,
                     user: users_schemas.User = fastapi.Depends(is_authenticated),
                     db: _orm.Session = fastapi.Depends(get_db)):
    """Creates and returns a payment link"""
    await _get_organization(organization_id=organization_id, db=db, user=user)
    conversion = await _get_credit_wallet_conversion(currency=body.currency, db=db)
    if conversion is None:
        raise fastapi.HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Currency " + body.currency + " does not have a conversion rate")
    if body.amount <= 0:
        raise fastapi.HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Amount must be a positive number")
    wallet = db.query(wallet_models.Wallet).filter_by(organization_id=organization_id).first()
    if wallet is None:
        raise fastapi.HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization does not have a wallet")
    else:
        # prevents two payments with same transaction reference
        # uniqueStr = ''.join([random.choice("qwertyuiopasdfghjklzxcvbnm1234567890") for x in range(10)])
        uniqueStr = time.time()
        txRef = user.id + "-" + organization_id + "-" + str(uniqueStr)
        rootUrl = config('API_URL')
        redirectUrl = rootUrl + '/credits/callback'
        link = await generate_payment_link(front_end_redirect_url=body.redirect_url, api_redirect_url=redirectUrl,
                                           user=user,
                                           amount=body.amount,
                                           currency=body.currency, tx_ref=txRef)
        return {"link": link}


############
# Services #
############

async def _get_organization(organization_id: str, db: _orm.Session,
                            user: users_schemas.User):
    organization = (
        db.query(organisation_models.Organization)
            .filter_by(creator=user.id)
            .filter(organisation_models.Organization.id == organization_id)
            .first()
    )

    if organization is None:
        raise fastapi.HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization does not exist")

    return organization


async def _get_credit_wallet_conversion(currency: str, db: _orm.Session):
    conversion = (
        db.query(credit_wallet_conversion_models.CreditWalletConversion)
            .filter_by(currency_code=currency)
            .first()
    )

    return conversion


async def _get_wallet(organization_id: str, currency: str, db: _orm.Session):
    wallet = db.query(wallet_models.Wallet).filter_by(organization_id=organization_id).filter_by(
        currency_code=currency).first()
    if wallet is None:
        wallet = wallet_models.Wallet(id=uuid4().hex, organization_id=organization_id, balance=0,
                                      currency_code=currency,
                                      last_updated=_dt.datetime.utcnow())

        db.add(wallet)
        db.commit()
        db.refresh(wallet)

    return wallet


async def _get_credit(organization_id: str,
                      user: users_schemas.User,
                      db: _orm.Session):
    # verify if the organization exists under the user's account

    await _get_organization(organization_id=organization_id, user=user, db=db)

    credit = db.query(model.CreditWallet).filter_by(organization_id=organization_id).first()
    if credit is None:
        credit = model.CreditWallet(id=uuid4().hex, organization_id=organization_id, amount=0,
                                    last_updated=_dt.datetime.utcnow())

        db.add(credit)
        db.commit()
        db.refresh(credit)

    return credit


async def _update_wallet(wallet, amount: float, db: _orm.Session, currency: str, tx_ref: str):
    # create a wallet transaction
    wallet_transaction = wallet_transaction_models.WalletTransaction(id=uuid4().hex, wallet_id=wallet.id,
                                                                     currency_code=currency, amount=amount,
                                                                     transaction_date=_dt.datetime.utcnow(),
                                                                     transaction_ref=tx_ref)
    db.add(wallet_transaction)
    db.commit()
    db.refresh(wallet_transaction)

    # update the wallet
    wallet.balance += amount
    wallet.last_updated = _dt.datetime.utcnow()
    db.commit()
    db.refresh(wallet)
    return wallet


add_pagination(app)
