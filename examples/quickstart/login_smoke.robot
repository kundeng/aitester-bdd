*** Settings ***
Documentation     Smoke test: login + open a case + verify it renders
...               Run against: make dev (http://localhost:5173 / :5175)
Library           aitester_bdd.AITester
Suite Setup       Given I start verification "${DEPLOYMENT}"
Suite Teardown    Then I finalize verification

*** Variables ***
${DEPLOYMENT}       prismi3-dev-smoke
${BASE_URL}         http://localhost:5173
${ADMIN_USER}       admin
${ADMIN_PASSWORD}   admin
${CASE_ID}          MAIN-0168

*** Test Cases ***
Login And Open Case
    [Setup]    Given I start scenario "login_open_case" at "${BASE_URL}"
    I define rule "login"
        When I open "${BASE_URL}/#/login"
        When I type "${ADMIN_USER}" into locator "input[name='username']"
        When I type secret "${ADMIN_PASSWORD}" into locator "input[name='password']"
        When I click locator "button[type='submit']"
        And url contains "/#/"
    I define rule "open_case"
        And I declare parents "login"
        When I open "${BASE_URL}/#/case/${CASE_ID}"
        And selector "h1" exists
        Then locator "h1" contains "Retryable"
    I define rule "case_has_tags"
        And I declare parents "open_case"
        Then count of locator "[data-testid='case-tag'], .tag-chip" is at least 1
